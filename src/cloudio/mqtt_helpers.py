# -*- coding: utf-8 -*-

import os, time
from threading import Thread, RLock, Event
import logging
import traceback
from abc import ABCMeta, abstractmethod
import paho.mqtt.client as mqtt     # pip install paho-mqtt
import ssl

class MqttAsyncClient():
    """Mimic the behavior of the java.MqttAsyncClient class"""

    # Errors from mqtt module - mirrored into this class
    MQTT_ERR_SUCCESS = mqtt.MQTT_ERR_SUCCESS

    def __init__(self, host, clientId='', clean_session=True, options=None):
        self._isConnected = False
        self._host = host
        self._onConnectCallback = None
        self._onDisconnectCallback = None
        self._onMessageCallback = None
        self._client = None
        self._clientLock = RLock()   # Protects access to _client attribute

        # Store mqtt client parameter for potential later reconnection
        # to cloud.iO
        self._clientClientId = clientId
        self._clientCleanSession = clean_session

    def _createMqttClient(self):
        self._clientLock.acquire()
        if self._client is None:
            if self._clientClientId:
                self._client = mqtt.Client(client_id=self._clientClientId,
                                           clean_session=self._clientCleanSession)
            else:
                self._client = mqtt.Client()

            self._client.on_connect = self.onConnect
            self._client.on_disconnect = self.onDisconnect
            self._client.on_message = self.onMessage
        self._clientLock.release()

    def setOnConnectCallback(self, onConnectCallback):
        self._onConnectCallback = onConnectCallback

    def setOnDisconnectCallback(self, onDisconnectCallback):
        self._onDisconnectCallback = onDisconnectCallback

    def setOnMessageCallback(self, onMessageCallback):
        self._onMessageCallback = onMessageCallback

    def connect(self, options):
        port = 1883 # Default port without ssl

        if options._caFile:
            # Check if file exists
            if not os.path.isfile(options._caFile):
                raise RuntimeError(u'CA file \'%s\' does not exist!' % options._caFile)

        clientCertFile = None
        if options._clientCertFile:
            # Check if file exists
            if not os.path.isfile(options._clientCertFile):
                raise RuntimeError(u'Client certificate file \'%s\' does not exist!' % options._clientCertFile)
            else:
                clientCertFile = options._clientCertFile

        clientKeyFile = None
        if options._clientKeyFile:
            # Check if file exists
            if not os.path.isfile(options._clientKeyFile):
                raise RuntimeError(u'Client private key file \'%s\' does not exist!' % options._clientKeyFile)
            else:
                clientKeyFile = options._clientKeyFile

        self._clientLock.acquire()  # Protect _client attribute

        # Create MQTT client if necessary
        self._createMqttClient()

        if options.will:
            self._client.will_set(options.will['topic'],
                                  options.will['message'],
                                  options.will['qos'],
                                  options.will['retained'])
        if self._client:
            password = options._password
            if not options._password:
                # paho client v1.3 and higher do no more accept '' as empty string. Need None
                password = None
            self._client.username_pw_set(options._username, password=password)

            if clientCertFile:
                port = 8883 # Port with ssl
                self._client.tls_set(options._caFile,  # CA certificate
                                    certfile=clientCertFile,  # Client certificate
                                    keyfile=clientKeyFile,  # Client private key
                                    tls_version=ssl.PROTOCOL_TLSv1,  # ssl.PROTOCOL_TLSv1, ssl.PROTOCOL_TLSv1_2
                                    ciphers=None)      # None, 'ALL', 'TLSv1.2', 'TLSv1.0'
                self._client.tls_insecure_set(True)  # True: No verification of the server hostname in the server certificate
            try:
                self._client.connect(self._host, port=port)
                self._client.loop_start()
                #time.sleep(1)   # Wait a bit for the callback onConnect to be called
            except Exception as e:
                pass
        self._clientLock.release()

    def disconnect(self):
        """Disconnects MQTT client
        """
        self._isConnected = False

        self._clientLock.acquire()
        # Stop MQTT client if still running
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self._clientLock.release()

    def isConnected(self):
        return self._isConnected

    def onConnect(self, client, userdata, flags, rc):
        if rc == 0:
            self._isConnected = True
            print u'Info: Connection to cloudio broker established.'
            if self._onConnectCallback:
                self._onConnectCallback()
        else:
            if rc == 1:
                print u'Error: Connection refused - incorrect protocol version'
            elif rc == 2:
                print u'Error: Connection refused - invalid client identifier'
            elif rc == 3:
                print u'Error: Connection refused - server unavailable'
            elif rc == 4:
                print u'Error: Connection refused - bad username or password'
            elif rc == 5:
                print u'Error: Connection refused - not authorised'
            else:
                print u'Error: Connection refused - unknown reason'

    def onDisconnect(self, client, userdata, rc):
        print 'Disconnect: %d' % rc

        self.disconnect()

        # Notify container class if disconnect callback
        # was registered.
        if self._onDisconnectCallback:
            self._onDisconnectCallback(rc)

    def onMessage(self, client, userdata, msg):
        # Delegate to container class
        if self._onMessageCallback:
            self._onMessageCallback(client, userdata, msg)

    def publish(self, topic, payload=None, qos=0, retain=False):
        timeout = 2.0
        message_info = self._client.publish(topic, payload, qos, retain)

        # Cannot use message_info.wait_for_publish() because it is blocking and
        # has no timeout parameter
        #message_info.wait_for_publish()
        #
        # Poll is_published() method
        while timeout > 0:
            if message_info.is_published():
                break
            timeout -= 0.1
            time.sleep(0.1)

        return message_info.is_published()

    def subscribe(self, topic, qos=0):
        return self._client.subscribe(topic, qos)

class MqttReconnectClient(MqttAsyncClient):
    """Same as MqttAsyncClient, but adds reconnect feature.
    """

    log = logging.getLogger(__name__)

    def __init__(self, host, clientId='', clean_session=True, options=None):
        MqttAsyncClient.__init__(self, host, clientId, clean_session, options)

        # options are not used by MqttAsyncClient store them in this class
        self._options = options
        self._onConnectedCallback = None
        self._onConnectionThreadFinishedCallback = None
        self._retryInterval = 10                # Connect retry interval in seconds
        self._autoReconnect = True
        self.thread = None
        self._connectTimeoutEvent = Event()
        self._connectionThreadLooping = True    # Set to false in case the connection thread should leave

        # Register callback method to be called when connection to cloud.iO gets established
        MqttAsyncClient.setOnConnectCallback(self, self._onConnect)

        # Register callback method to be called when connection to cloud.iO gets lost
        MqttAsyncClient.setOnDisconnectCallback(self, self._onDisconnect)

    def setOnConnectCallback(self, onConnect):
        assert False, u'Not allowed in this class!'

    def setOnDisconnectCallback(self, onDisconnect):
        assert False, u'Not allowed in this class!'

    def setOnConnectedCallback(self, onConnectedCallback):
        self._onConnectedCallback = onConnectedCallback

    def setOnConnectionThreadFinishedCallback(self, onConnectionThreadFinishedCallback):
        self._onConnectionThreadFinishedCallback = onConnectionThreadFinishedCallback

    def start(self):
        self._startConnectionThread()

    def stop(self):
        self._autoReconnect = False
        self.disconnect()

    def _startConnectionThread(self):
        if self.thread and self.thread.isAlive():
            self.log.warning('Mqtt client connection thread already/still running!')

        self._stopConnectionThread()

        self.thread = Thread(target=self._run, name='mqtt-reconnect-client-' + self._clientClientId)
        # Close thread as soon as main thread exits
        self.thread.setDaemon(True)

        self._connectionThreadLooping = True
        self.thread.start()

    def _stopConnectionThread(self):
        if self.thread:
            self._connectionThreadLooping = False
            self.thread.join()
            self.thread = None

    def _onConnect(self):
        self._connectTimeoutEvent.set() # Free the connection thread

    def _onDisconnect(self, rc):
        if self._autoReconnect:
            self._startConnectionThread()

    def _onConnected(self):
        if self._onConnectedCallback:
            self._onConnectedCallback()

    def _onConnectionThreadFinished(self):
        if self._onConnectionThreadFinishedCallback:
            self._onConnectionThreadFinishedCallback()

    ######################################################################
    # Active part
    #
    def _run(self):
        """Called by the internal thread"""

        self.log.info(u'Mqtt client reconnect thread running...')

        while not self.isConnected() and self._connectionThreadLooping:
            try:
                self._connectTimeoutEvent.clear() # Reset connect timeout event prior to connect
                self.log.info(u'Trying to connect to cloud.iO...')
                self.connect(self._options)
            except Exception as exception:
                traceback.print_exc()
                print u'Error during broker connect!'
                exit(1)

            # Check if thread should leave
            if not self._connectionThreadLooping:
                # Tell subscriber connection thread has finished
                self._onConnectionThreadFinished()
                return

            if not self.isConnected():
                # If we should not retry, give up
                if self._retryInterval > 0:
                    # Wait until it is time for the next connect
                    self._connectTimeoutEvent.wait(self._retryInterval)

                # If we should not retry, give up
                if self._retryInterval == 0:
                    break

        if self.isConnected():
            self.log.info(u'Connected to cloud.iO broker')

            # Tell subscriber we are connected
            self._onConnected()

        # Tell subscriber connection thread has finished
        self._onConnectionThreadFinished()


class MqttConnectOptions():
    def __init__(self):
        self._username = ''
        self._password = ''
        self._caFile = None                 # type: str
        self._clientCertFile = None         # type: str
        self._clientKeyFile = None          # type: str
        self.will = None                    # type dict

    def setWill(self, topic, message, qos, retained):
        self.will = {}
        self.will['topic'] = topic
        self.will['message'] = message
        self.will['qos'] = qos
        self.will['retained'] = retained

class MqttClientPersistence(object):
    """Mimic the behavior of the java.MqttClientPersistence interface.

    Compatible with MQTT v3.

    See: https://www.eclipse.org/paho/files/javadoc/org/eclipse/paho/client/mqttv3/MqttClientPersistence.html
    """

    __metaclass__ = ABCMeta

    def __init__(self):
        pass

    def clear(self):
        """Clears persistence, so that it no longer contains any persisted data.
        """
        pass

    def close(self):
        """Close the persistent store that was previously opened.
        """
        pass

    def containsKey(self, key):
        """Returns whether or not data is persisted using the specified key.

        :param key The key for data, which was used when originally saving it.
        :type key str
        :return True if key is present.
        """
        pass

    def get(self, key):
        """Gets the specified data out of the persistent store.

        :param key The key for the data to be removed from the store.
        :type key str
        :return The wanted data.
        :type bytearray
        """
        pass

    def keys(self):
        """Returns an Enumeration over the keys in this persistent data store.

        :return: generator
        """
        pass

    def open(self, clientId, serverUri):
        """Initialise the persistent store.

        Initialise the persistent store. If a persistent store exists for this client ID then open it,
        otherwise create a new one. If the persistent store is already open then just return. An application
        may use the same client ID to connect to many different servers, so the client ID in conjunction
        with the connection will uniquely identify the persistence store required.

        :param clientId The client for which the persistent store should be opened.
        :type clientId str
        :param serverUri The connection string as specified when the MQTT client instance was created.
        :type serverUri str
        """
        pass

    def put(self, key, persistable):
        """Puts the specified data into the persistent store.

        :param key The key for the data, which will be used later to retrieve it.
        :type key str
        :param persistable The data to persist.
        :type persistable bool
        """
        pass

    def remove(self, key):
        """Remove the data for the specified key.

        :param key The key associated to the data to remove.
        :type key str
        :return None
        """
        pass

class MemoryPersistence(MqttClientPersistence):
    """Persistance store that uses memory.
    """
    def __init__(self):
        super(MemoryPersistence, self).__init__()
        self._persistance = {}

    def open(self, clientId, serverUri):
        pass

    def close(self):
        self.clear()

    def put(self, key, persistable):
        self._persistance[key] = persistable

    def get(self, key):
        if self._persistance.has_key(key):
            return self._persistance[key]

    def containsKey(self, key):
        return True if self._persistance.has_key(key) else False

    def keys(self):
        keys = []
        for key in self._persistance.iterkeys():
            keys.append(key)
        return keys

    def remove(self, key):
        # Remove the key if it exist. If it does not exist
        # leave silently
        self._persistance.pop(key, None)

    def clear(self):
        self._persistance.clear()