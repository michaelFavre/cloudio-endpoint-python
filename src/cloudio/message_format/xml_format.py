from cloudio.interface.message_format import CloudioMessageFormat
from xml.dom import minidom
from utils import timestamp as timestamp_helpers

class XmlMessageFormat(CloudioMessageFormat):
    def __init__(self):
        pass

    def serializeEndpoint(self, endpoint):
        print("serialize endpoint")
        pass

    def serializeNode(self, node):
        print("serialize node")
        pass

    def serializeAttribute(self, attribute):
        print("serialize attribute")
        pass

    def deserializeAttribute(self, data, attribute):
        timestamp = timestamp_helpers.getTimeInMilliseconds()
        attribute.setValueFromCloud(data, timestamp)
        pass