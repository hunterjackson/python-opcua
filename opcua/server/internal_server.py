"""
Internal server implementing opcu-ua interface. can be used on server side or to implement binary/https opc-ua servers
"""

from datetime import datetime
from copy import copy, deepcopy
from datetime import timedelta
from os import path
import logging
from threading import Lock
from enum import Enum
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse


from opcua import ua
from opcua.common import utils
from opcua.common.node import Node
from opcua.server.history import HistoryManager
from opcua.server.address_space import AddressSpace
from opcua.server.address_space import AttributeService
from opcua.server.address_space import ViewService
from opcua.server.address_space import NodeManagementService
from opcua.server.address_space import MethodService
from opcua.server.subscription_service import SubscriptionService
from opcua.server.standard_address_space import standard_address_space
from opcua.server.users import User
from opcua.common import xmlimporter


class SessionState(Enum):
    Created = 0
    Activated = 1
    Closed = 2


class ServerDesc(object):
    def __init__(self, serv, cap=None):
        self.Server = serv
        self.Capabilities = cap


class InternalServer(object):

    def __init__(self, cacheFile = None):
        self.logger = logging.getLogger(__name__)
        self.endpoints = []
        self._channel_id_counter = 5
        self.allow_remote_admin = True
        self.disabled_clock = False  # for debugging we may want to disable clock that writes too much in log
        self._known_servers = {}  # used if we are a discovery server

        self.aspace = AddressSpace()
        self.attribute_service = AttributeService(self.aspace)
        self.view_service = ViewService(self.aspace)
        self.method_service = MethodService(self.aspace)
        self.node_mgt_service = NodeManagementService(self.aspace)

        if cacheFile and path.isfile(cacheFile):
            # import address space from shelve
            self.aspace.load(cacheFile)
        else:
            # import address space from code generated from xml
            standard_address_space.fill_address_space(self.node_mgt_service)
            # import address space directly from xml, this has preformance impact so disabled
            #importer = xmlimporter.XmlImporter(self.node_mgt_service)
            #importer.import_xml("/path/to/python-opcua/schemas/Opc.Ua.NodeSet2.xml")

            if cacheFile:
                self.aspace.dump(cacheFile)

        self.loop = utils.ThreadLoop()
        self.asyncio_transports = []
        self.subscription_service = SubscriptionService(self.loop, self.aspace)

        self.history_manager = HistoryManager(self)

        # create a session to use on server side
        self.isession = InternalSession(self, self.aspace, self.subscription_service, "Internal", user=User.Admin)
        self.current_time_node = Node(self.isession, ua.NodeId(ua.ObjectIds.Server_ServerStatus_CurrentTime))
        uries = ["http://opcfoundation.org/UA/"]
        ns_node = Node(self.isession, ua.NodeId(ua.ObjectIds.Server_NamespaceArray))
        ns_node.set_value(uries)

    def load_address_space(self, path):
        self.aspace.load(path)

    def dump_address_space(self, path):
        self.aspace.dump(path)

    def start(self):
        self.logger.info("starting internal server")
        for edp in self.endpoints:
            self._known_servers[edp.Server.ApplicationUri] = ServerDesc(edp.Server)
        self.loop.start()
        Node(self.isession, ua.NodeId(ua.ObjectIds.Server_ServerStatus_State)).set_value(0)
        Node(self.isession, ua.NodeId(ua.ObjectIds.Server_ServerStatus_StartTime)).set_value(datetime.utcnow())
        if not self.disabled_clock:
            self._set_current_time()

    def stop(self):
        self.logger.info("stopping internal server")
        self.loop.stop()
        self.history_manager.stop()

    def _set_current_time(self):
        self.current_time_node.set_value(datetime.utcnow())
        self.loop.call_later(1, self._set_current_time)

    def get_new_channel_id(self):
        self._channel_id_counter += 1
        return self._channel_id_counter

    def add_endpoint(self, endpoint):
        self.endpoints.append(endpoint)

    def get_endpoints(self, params=None, sockname=None):
        self.logger.info("get endpoint")
        if sockname:
            #return to client the ip address it has access to
            edps = []
            for edp in self.endpoints:
                edp1 = copy(edp)
                url = urlparse(edp1.EndpointUrl)
                url = url._replace(netloc=sockname[0] + ":" + str(sockname[1]))
                edp1.EndpointUrl = url.geturl()
                edps.append(edp1)
            return edps
        return self.endpoints[:]

    def find_servers(self, params):
        if not params.ServerUris:
            return [desc.Server for desc in self._known_servers.values()]
        servers = []
        for serv in self._known_servers.values():
            serv_uri = serv.Server.ApplicationUri.split(":")
            for uri in params.ServerUris:
                uri = uri.split(":")
                if serv_uri[:len(uri)] == uri:
                    servers.append(serv.Server)
                    break
        return servers

    def register_server(self, server, conf=None):
        appdesc = ua.ApplicationDescription()
        appdesc.ApplicationUri = server.ServerUri
        appdesc.ProductUri = server.ProductUri
        appdesc.ApplicationName = server.ServerNames[0]  # FIXME: select name from client locale
        appdesc.ApplicationType = server.ServerType
        appdesc.GatewayServerUri = server.GatewayServerUri
        appdesc.DiscoveryUrls = server.DiscoveryUrls  # FIXME: select discovery uri using reachability from client network
        self._known_servers[server.ServerUri] = ServerDesc(appdesc, conf)

    def register_server2(self, params):
        return self.register_server(params.Server, params.DiscoveryConfiguration)

    def create_session(self, name, user=User.Anonymous, external=False):
        return InternalSession(self, self.aspace, self.subscription_service, name, user=user, external=external)

    def enable_history_data_change(self, node, period=timedelta(days=7), count=0):
        """
        Set attribute Historizing of node to True and start storing data for history
        """
        node.set_attribute(ua.AttributeIds.Historizing, ua.DataValue(True))
        node.set_attr_bit(ua.AttributeIds.AccessLevel, ua.AccessLevel.HistoryRead)
        node.set_attr_bit(ua.AttributeIds.UserAccessLevel, ua.AccessLevel.HistoryRead)
        self.history_manager.historize_data_change(node, period, count)

    def disable_history_data_change(self, node):
        """
        Set attribute Historizing of node to False and stop storing data for history
        """
        node.set_attribute(ua.AttributeIds.Historizing, ua.DataValue(False))
        node.unset_attr_bit(ua.AttributeIds.AccessLevel, ua.AccessLevel.HistoryRead)
        node.unset_attr_bit(ua.AttributeIds.UserAccessLevel, ua.AccessLevel.HistoryRead)
        self.history_manager.dehistorize(node)

    def enable_history_event(self, source, period=timedelta(days=7), count=0):
        """
        Set attribute History Read of object events to True and start storing data for history
        """
        # to historize events of an object, first check if object supports events
        source_event_notifier = source.get_attribute(ua.AttributeIds.EventNotifier)
        if source_event_notifier.Value.Value & 1 == 1:  # check bit 0
            # if it supports events, turn on bit 2 (enables history read)
            source.set_attr_bit(ua.AttributeIds.EventNotifier, 2)
            # send the object to history manager
            self.history_manager.historize_event(source, period, count)

    def disable_history_event(self, source):
        """
        Set attribute History Read of node to False and stop storing data for history
        """
        source.unset_attr_bit(ua.AttributeIds.EventNotifier, 2)
        self.history_manager.dehistorize(source)


class InternalSession(object):
    _counter = 10
    _auth_counter = 1000

    def __init__(self, internal_server, aspace, submgr, name, user=User.Anonymous, external=False):
        self.logger = logging.getLogger(__name__)
        self.iserver = internal_server
        self.external = external  # define if session is external, we need to copy some objects if it is internal
        self.aspace = aspace
        self.subscription_service = submgr
        self.name = name
        self.user = user
        self.nonce = None
        self.state = SessionState.Created
        self.session_id = ua.NodeId(self._counter)
        InternalSession._counter += 1
        self.authentication_token = ua.NodeId(self._auth_counter)
        InternalSession._auth_counter += 1
        self.subscriptions = []
        self.logger.info("Created internal session %s", self.name)
        self._lock = Lock()

    def __str__(self):
        return "InternalSession(name:{}, user:{}, id:{}, auth_token:{})".format(self.name, self.user, self.session_id, self.authentication_token)

    def get_endpoints(self, params=None, sockname=None):
        return self.iserver.get_endpoints(params, sockname)

    def create_session(self, params, sockname=None):
        self.logger.info("Create session request")

        result = ua.CreateSessionResult()
        result.SessionId = self.session_id
        result.AuthenticationToken = self.authentication_token
        result.RevisedSessionTimeout = params.RequestedSessionTimeout
        result.MaxRequestMessageSize = 65536
        self.nonce = utils.create_nonce(32)
        result.ServerNonce = self.nonce
        result.ServerEndpoints = self.get_endpoints(sockname=sockname)

        return result

    def close_session(self, delete_subs):
        self.logger.info("close session %s with subscriptions %s", self, self.subscriptions)
        self.state = SessionState.Closed
        self.delete_subscriptions(self.subscriptions[:])

    def activate_session(self, params):
        self.logger.info("activate session")
        result = ua.ActivateSessionResult()
        if self.state != SessionState.Created:
            raise utils.ServiceError(ua.StatusCodes.BadSessionIdInvalid)
        self.nonce = utils.create_nonce(32)
        result.ServerNonce = self.nonce
        for _ in params.ClientSoftwareCertificates:
            result.Results.append(ua.StatusCode())
        self.state = SessionState.Activated
        id_token = params.UserIdentityToken
        if isinstance(id_token, ua.UserNameIdentityToken):
            if self.iserver.allow_remote_admin and id_token.UserName in ("admin", "Admin"):
                self.user = User.Admin
        self.logger.info("Activated internal session %s for user %s", self.name, self.user)
        return result

    def read(self, params):
        results = self.iserver.attribute_service.read(params)
        if self.external:
            return results
        return [deepcopy(dv) for dv in results]

    def history_read(self, params):
        return self.iserver.history_manager.read_history(params)

    def write(self, params):
        if not self.external:
            # If session is internal we need to store a copy og object, not a reference,
            #otherwise users may change it and we will not generate expected events
            params.NodesToWrite = [deepcopy(ntw) for ntw in params.NodesToWrite]
        return self.iserver.attribute_service.write(params, self.user)

    def browse(self, params):
        return self.iserver.view_service.browse(params)

    def translate_browsepaths_to_nodeids(self, params):
        return self.iserver.view_service.translate_browsepaths_to_nodeids(params)

    def add_nodes(self, params):
        return self.iserver.node_mgt_service.add_nodes(params, self.user)

    def delete_nodes(self, params):
        return self.iserver.node_mgt_service.delete_nodes(params, self.user)

    def add_references(self, params):
        return self.iserver.node_mgt_service.add_references(params, self.user)

    def delete_references(self, params):
        return self.iserver.node_mgt_service.delete_references(params, self.user)

    def add_method_callback(self, methodid, callback):
        return self.aspace.add_method_callback(methodid, callback)

    def call(self, params):
        return self.iserver.method_service.call(params)

    def create_subscription(self, params, callback):
        result = self.subscription_service.create_subscription(params, callback)
        with self._lock:
            self.subscriptions.append(result.SubscriptionId)
        return result

    def create_monitored_items(self, params):
        return self.subscription_service.create_monitored_items(params)

    def modify_monitored_items(self, params):
        return self.subscription_service.modify_monitored_items(params)

    def republish(self, params):
        return self.subscription_service.republish(params)

    def delete_subscriptions(self, ids):
        for i in ids:
            with self._lock:
                if i in self.subscriptions:
                    self.subscriptions.remove(i)
        return self.subscription_service.delete_subscriptions(ids)

    def delete_monitored_items(self, params):
        return self.subscription_service.delete_monitored_items(params)

    def publish(self, acks=None):
        if acks is None:
            acks = []
        return self.subscription_service.publish(acks)
