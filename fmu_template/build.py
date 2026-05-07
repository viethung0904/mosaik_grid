from component_model.model import Model
from fmpy.util import fmu_info
from fmpy import plot_result, simulate_fmu

asBuilt = Model.build("./ACLineSegment.py")
info = fmu_info(asBuilt.name)  # not necessary, but it lists essential properties of the FMU

result = simulate_fmu(
   "ACLineSegment.fmu",
   stop_time=10.0,
   step_size=1.0,
   validate=True,
   solver="Euler",
   debug_logging=True,
   logger=print,
)
plot_result(result)