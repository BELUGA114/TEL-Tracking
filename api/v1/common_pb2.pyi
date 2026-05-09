class Satellite:
    def __init__(self, norad_id: int = ..., name: str = ..., tle_ln1: str = ..., tle_ln2: str = ...) -> None: ...
    norad_id: int
    name: str
    tle_ln1: str
    tle_ln2: str

class EphemerisData:
    ds50_time: float
    mse_time: float
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
