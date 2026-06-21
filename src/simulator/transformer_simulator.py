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
        'Transformer': {
            'public': True,
            'params': ['r_ohm', 'x_ohm', 'rated_u1_kv', 'rated_u2_kv', 'phase_angle_clock'],
            'attrs': ['V1_mag', 'V1_angle', 'P2', 'Q2', 'P1', 'Q1', 'V2', 'dP_load', 'dQ_load', 'dQ_mag'],
        },
    },
}

class Transformer(mosaik_api.Simulator):
    def __init__(self):
        super().__init__(META)
        self.eid_counters = {}
        self.data = collections.defaultdict(dict)
        self._entities = {}
        self.unzipdir = None                # directory of FMU
        self.model_description = None       # model description of FMU
        self.model_name = None              # model name of FMU
        self.instance_name = None           # instance name of FMU
        self.vrs = {}                       # dict for FMU value references
        self.vr_inputs = None               # input variables
        self.vr_outputs = None              # output variables      
        self.step_size = 1                  # int simulation step size (must be 1 for each simulator)
        self.start_time = 0                 # FMPy parameter
        self.stop_time = 0                  # FMPy parameter
        self.stop_time_defined = False      # FMPy parameter
        self.sec_per_mt = 60                 # Number of seconds of internal time per mosaik time
        self.fmutimes = {}                  # Keeping track of each FMU's internal time
        self._phys_step = 0                 # Physical time step counter (incremented once per 60s step)
        self.eid = None

    def init(self, sid, time_resolution=1.0, fmu_filename=None, instance_name=None, step_size=None, 
            start_time = 0, stop_time = 0, seconds_per_mosaik_timestep = 1):
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

        self.vr_inputs = [self.vrs['V1_mag'], self.vrs['V1_angle']]  # Transformer inputs
        self.vr_outputs = [self.vrs['V2'], self.vrs['P2'], self.vrs['Q2'], self.vrs['P1'], self.vrs['Q1']]  # Output variables for transformer

        print(self.vrs)
        print("Initialization of FMU Transformer successful")

        return self.meta
    
    def create(self, num, model, r_ohm=0.0, x_ohm=0.0, rated_u1_kv=0.0, rated_u2_kv=0.0, phase_angle_clock=0):
        counter = self.eid_counters.setdefault(model, count())
        entities = []
        for i in range(num):
            eid = '%s_%s' % (model, next(counter))  # entity ID
            self.eid=eid
            
            # extract the FMU
            self.unzipdir = extract(self.model_name)
            
            # Use unique instance name for each entity
            unique_instance_name = f"{self.instance_name}_{eid}"

            if self.model_description.fmiVersion == '2.0':
                fmu = FMU2Slave(guid = self.model_description.guid,
                        unzipDirectory = self.unzipdir,
                        modelIdentifier = self.model_description.coSimulation.modelIdentifier,
                        instanceName = unique_instance_name)
                self._entities[eid] = fmu

                fmu.instantiate()
                self._entities[eid].setupExperiment(startTime = self.start_time)

                # Set transformer parameters (R, X in Ω; ratedU in V) before initialization
                self._entities[eid].enterInitializationMode()
                self._entities[eid].setReal([self.vrs['R']],               [r_ohm])
                self._entities[eid].setReal([self.vrs['X']],               [x_ohm])
                self._entities[eid].setReal([self.vrs['ratedU1']],         [rated_u1_kv * 1000.0])
                self._entities[eid].setReal([self.vrs['ratedU2']],         [rated_u2_kv * 1000.0])
                self._entities[eid].setReal([self.vrs['phaseAngleClock']], [float(phase_angle_clock)])
                self._entities[eid].exitInitializationMode()

                # Handling tracking internal fmu times
                self.fmutimes[eid] = self.start_time*self.sec_per_mt

                entities.append( { 'eid': eid, 'type': model, 'rel': [] } )
            
            elif self.model_description.fmiVersion == '3.0':
                fmu = FMU3Slave(
                guid=self.model_description.guid,
                unzipDirectory=self.unzipdir,
                modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                instanceName=unique_instance_name)

                self._entities[eid] = fmu
             
                # Initialize FMI 3.0 (exitInitializationMode automatically enters Step Mode)
                fmu.instantiate()
                fmu.enterInitializationMode(startTime=self.start_time)
                self._entities[eid].setFloat64([self.vrs['R']],               [r_ohm])
                self._entities[eid].setFloat64([self.vrs['X']],               [x_ohm])
                self._entities[eid].setFloat64([self.vrs['ratedU1']],         [rated_u1_kv * 1000.0])
                self._entities[eid].setFloat64([self.vrs['ratedU2']],         [rated_u2_kv * 1000.0])
                self._entities[eid].setFloat64([self.vrs['phaseAngleClock']], [float(phase_angle_clock)])
                fmu.exitInitializationMode()
                
                self.fmutimes[eid] = self.start_time*self.sec_per_mt

                entities.append( { 'eid': eid, 'type': model, 'rel': [] } )

        return entities

    def step(self, time, inputs, max_advance):
        target_time = (self._phys_step + 1) * 60.0   # 60 s per physical time step
        self._phys_step += 1
        
        for eid in self._entities.keys():
            if eid in inputs:
                entity_inputs = inputs[eid]
                if self.model_description.fmiVersion == '2.0':
                    if 'V1_mag' in entity_inputs:
                        v1_mag_values = entity_inputs['V1_mag']
                        if v1_mag_values:
                            v1_mag = sum(v1_mag_values.values()) * 1000.0  # kV → V
                            self._entities[eid].setReal([self.vrs['V1_mag']], [v1_mag])
                    if 'V1_angle' in entity_inputs:
                        v1_angle_values = entity_inputs['V1_angle']
                        if v1_angle_values:
                            self._entities[eid].setReal([self.vrs['V1_angle']], [sum(v1_angle_values.values())])
                    if 'P2' in entity_inputs:
                        p2_values = entity_inputs['P2']
                        if p2_values:
                            self._entities[eid].setReal([self.vrs['P2']], [sum(p2_values.values())])
                    if 'Q2' in entity_inputs:
                        q2_values = entity_inputs['Q2']
                        if q2_values:
                            self._entities[eid].setReal([self.vrs['Q2']], [sum(q2_values.values())])
                elif self.model_description.fmiVersion == '3.0':
                    if 'V1_mag' in entity_inputs:
                        v1_mag_values = entity_inputs['V1_mag']
                        if v1_mag_values:
                            v1_mag = sum(v1_mag_values.values()) * 1000.0  # kV → V
                            self._entities[eid].setFloat64([self.vrs['V1_mag']], [v1_mag])
                    if 'V1_angle' in entity_inputs:
                        v1_angle_values = entity_inputs['V1_angle']
                        if v1_angle_values:
                            self._entities[eid].setFloat64([self.vrs['V1_angle']], [sum(v1_angle_values.values())])
                    if 'P2' in entity_inputs:
                        p2_values = entity_inputs['P2']
                        if p2_values:
                            self._entities[eid].setFloat64([self.vrs['P2']], [sum(p2_values.values())])
                    if 'Q2' in entity_inputs:
                        q2_values = entity_inputs['Q2']
                        if q2_values:
                            self._entities[eid].setFloat64([self.vrs['Q2']], [sum(q2_values.values())])

            communication_point = self.fmutimes[eid]
            communication_step_size = target_time - communication_point
            self._entities[eid].doStep(currentCommunicationPoint=communication_point,
                                       communicationStepSize=communication_step_size)
            self.fmutimes[eid] += communication_step_size

        return time + self.step_size  # Return next Mosaik time step
    
    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            if eid in self._entities:
                data[eid] = {}
                for attr in attrs:
                    if self.model_description.fmiVersion == '2.0':
                        if attr == 'P1':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['P1']])[0]
                        if attr == 'Q1':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['Q1']])[0]
                        if attr == 'V2':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['V2']])[0]
                        if attr == 'dP_load':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['dP_load']])[0]
                        if attr == 'dQ_load':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['dQ_load']])[0]
                        if attr == 'dQ_mag':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['dQ_mag']])[0]
                    elif self.model_description.fmiVersion == '3.0':
                        if attr == 'P1':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['P1']])[0]
                        if attr == 'Q1':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['Q1']])[0]
                        if attr == 'V2':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['V2']])[0]
                        if attr == 'dP_load':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['dP_load']])[0]
                        if attr == 'dQ_load':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['dQ_load']])[0]
                        if attr == 'dQ_mag':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['dQ_mag']])[0]

        return data