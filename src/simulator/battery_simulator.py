import mosaik_api
from fmpy import read_model_description, extract
from fmpy.fmi2 import FMU2Slave
from fmpy.fmi3 import FMU3Slave
import collections
from itertools import count

META = {
    "api_version": "3.0",
    "type": "hybrid",
    'models': {
        'Battery': {
            'public': True,
            'params': ['p_charge_mw', 'power_factor'],
            'attrs': ['P_load_mw', 'Q_load_mvar', 'SOC', 'V_volt', 'I_amp'],
        },
    },
}


class Battery(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self.eid_counters = {}
        self.data = collections.defaultdict(dict)
        self._entities = {}
        self.unzipdir = None
        self.model_description = None
        self.model_name = None
        self.instance_name = None
        self.vrs = {}
        self.step_size = 1
        self.start_time = 0
        self.stop_time = 0
        self.sec_per_mt = 60
        self.fmutimes = {}
        self._phys_step = 0
        # Real P_charge setpoints per entity. NOT applied during init — see create().
        self._p_charge_w = {}
        # Constant power-factor setpoint per entity (CIM PowerElectronicsConnection
        # controlMode=constantPowerFactor). Applied every step, same as P_charge.
        self._power_factor = {}

    def init(self, sid, time_resolution=1.0, fmu_filename=None, instance_name=None,
             step_size=None, start_time=0, stop_time=0, seconds_per_mosaik_timestep=1):
        self.start_time = start_time
        self.stop_time = stop_time
        self.step_size = step_size
        self.model_name = fmu_filename
        self.instance_name = instance_name

        self.model_description = read_model_description(fmu_filename)
        assert self.model_description is not None

        for variable in self.model_description.modelVariables:
            self.vrs[variable.name] = variable.valueReference
        assert self.vrs is not None

        print(self.vrs)
        print("Initialization of FMU Battery successful")

        return self.meta

    def create(self, num, model, p_charge_mw=0.0, power_factor=1.0):
        counter = self.eid_counters.setdefault(model, count())
        entities = []

        for i in range(num):
            eid = '%s_%s' % (model, next(counter))

            self.unzipdir = extract(self.model_name)
            unique_instance_name = f"{self.instance_name}_{eid}"

            if self.model_description.fmiVersion == '2.0':
                fmu = FMU2Slave(
                    guid=self.model_description.guid,
                    unzipDirectory=self.unzipdir,
                    modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                    instanceName=unique_instance_name)

                self._entities[eid] = fmu

                fmu.instantiate()
                fmu.setupExperiment(startTime=self.start_time)
                fmu.enterInitializationMode()
                # P_charge = 0 so exitInitializationMode's Simulink call is a
                # no-op (P_net = 0, I = 0, ΔSOC = 0). The real setpoint is
                # stored and applied before the first doStep in step().
                fmu.setReal([self.vrs['P_charge']], [0.0])
                fmu.setReal([self.vrs['P_load']],   [0.0])
                fmu.exitInitializationMode()

                self._p_charge_w[eid] = p_charge_mw * 1e6
                self._power_factor[eid] = power_factor
                self.fmutimes[eid] = self.start_time * self.sec_per_mt
                self.data[eid] = {'P_load_mw': 0.0, 'Q_load_mvar': 0.0, 'SOC': 90.0, 'V_volt': 0.0, 'I_amp': 0.0}
                entities.append({'eid': eid, 'type': model, 'rel': []})

            elif self.model_description.fmiVersion == '3.0':
                fmu = FMU3Slave(
                    guid=self.model_description.guid,
                    unzipDirectory=self.unzipdir,
                    modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                    instanceName=unique_instance_name)

                self._entities[eid] = fmu

                fmu.instantiate()
                fmu.enterInitializationMode(startTime=self.start_time)
                # Same reason as FMI 2.0 above.
                fmu.setFloat64([self.vrs['P_charge']], [0.0])
                fmu.setFloat64([self.vrs['P_load']],   [0.0])
                fmu.exitInitializationMode()

                self._p_charge_w[eid] = p_charge_mw * 1e6
                self._power_factor[eid] = power_factor
                self.fmutimes[eid] = self.start_time * self.sec_per_mt
                self.data[eid] = {'P_load_mw': 0.0, 'Q_load_mvar': 0.0, 'SOC': 90.0, 'V_volt': 0.0, 'I_amp': 0.0}
                entities.append({'eid': eid, 'type': model, 'rel': []})

        return entities

    def step(self, time, inputs, max_advance):
        target_time = (self._phys_step + 1) * 60.0
        self._phys_step += 1

        for eid, fmu in self._entities.items():
            is_fmi3 = self.model_description.fmiVersion == '3.0'

            # Apply the real P_charge before stepping. Done here (not in create)
            # to avoid the spurious init-mode SOC advance described above.
            pf = self._power_factor.get(eid, 1.0)
            if is_fmi3:
                fmu.setFloat64([self.vrs['P_charge']], [self._p_charge_w.get(eid, 0.0)])
                fmu.setFloat64([self.vrs['P_load']],   [0.0])
                fmu.setFloat64([self.vrs['Power_factor']], [pf])
            else:
                fmu.setReal([self.vrs['P_charge']], [self._p_charge_w.get(eid, 0.0)])
                fmu.setReal([self.vrs['P_load']],   [0.0])
                fmu.setReal([self.vrs['Power_factor']], [pf])

            communication_point = self.fmutimes[eid]
            communication_step_size = target_time - communication_point

            fmu.doStep(
                currentCommunicationPoint=communication_point,
                communicationStepSize=communication_step_size)

            self.fmutimes[eid] += communication_step_size

        return time + self.step_size

    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            if eid not in self._entities:
                continue
            is_fmi3 = self.model_description.fmiVersion == '3.0'
            data[eid] = {}
            for attr in attrs:
                if attr == 'P_load_mw':
                    p_w = (self._entities[eid].getFloat64([self.vrs['P']])[0] if is_fmi3
                           else self._entities[eid].getReal([self.vrs['P']])[0])
                    data[eid]['P_load_mw'] = -p_w / 1e6

                elif attr == 'Q_load_mvar':
                    q_var = (self._entities[eid].getFloat64([self.vrs['Q']])[0] if is_fmi3
                             else self._entities[eid].getReal([self.vrs['Q']])[0])
                    data[eid]['Q_load_mvar'] = -q_var / 1e6

                elif attr == 'SOC':
                    data[eid]['SOC'] = (self._entities[eid].getFloat64([self.vrs['SOC']])[0] if is_fmi3
                                        else self._entities[eid].getReal([self.vrs['SOC']])[0])

                elif attr == 'V_volt':
                    data[eid]['V_volt'] = (self._entities[eid].getFloat64([self.vrs['V']])[0] if is_fmi3
                                           else self._entities[eid].getReal([self.vrs['V']])[0])

                elif attr == 'I_amp':
                    data[eid]['I_amp'] = (self._entities[eid].getFloat64([self.vrs['I']])[0] if is_fmi3
                                          else self._entities[eid].getReal([self.vrs['I']])[0])

        return data

    def finalize(self):
        for fmu in self._entities.values():
            try:
                fmu.terminate()
                fmu.freeInstance()
            except Exception:
                pass
