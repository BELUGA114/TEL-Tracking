from google.protobuf.timestamp_pb2 import Timestamp

class InfoResponse:
    name: str
    version: str
    commit: str
    build_date: str
    astro_std_lib_info: str
    sgp4_lib_info: str
    timestamp: Timestamp
