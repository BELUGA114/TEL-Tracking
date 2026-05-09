from api.v1.common_pb2 import Satellite, EphemerisData
from google.protobuf.timestamp_pb2 import Timestamp

class EphemTimeGrid:
    time_start_ds50: float
    time_start_utc: Timestamp
    time_end_ds50: float
    time_end_utc: Timestamp

class EphemTask:
    def __init__(self, task_id: int = ..., sat: Satellite = ..., time_grid: EphemTimeGrid = ...) -> None: ...
    task_id: int
    sat: Satellite
    time_grid: EphemTimeGrid

class EphemRequest:
    def __init__(self, req_id: int = ..., ephem_type: int = ..., common_time_grid: EphemTimeGrid = ..., tasks: list[EphemTask] = ...) -> None: ...
    req_id: int
    ephem_type: int
    common_time_grid: EphemTimeGrid
    tasks: list[EphemTask]

class EphemOut:
    task_id: int
    ephem_data: list[EphemerisData]
    ephem_points_count: int

class EphemResponse:
    req_id: int
    stream_id: int
    stream_chunk_id: int
    result: EphemOut

EphemPlaceholder: int
EphemEci: int
EphemJ2K: int
