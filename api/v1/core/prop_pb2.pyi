from api.v1.common_pb2 import Satellite, EphemerisData
from google.protobuf.timestamp_pb2 import Timestamp

class PropTask:
    def __init__(self, sat: Satellite = ..., time: float = ..., time_utc: Timestamp = ...) -> None: ...
    sat: Satellite
    time: float
    time_utc: Timestamp

class PropRequest:
    def __init__(self, req_id: int = ..., time_type: int = ..., task: PropTask = ...) -> None: ...
    req_id: int
    time_type: int
    task: PropTask

class PropResponse:
    req_id: int
    result: EphemerisData

TimeMse: int
TimeDs50: int
