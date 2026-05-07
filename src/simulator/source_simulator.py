import mosaik_api
from itertools import count
from fmpy import read_model_description, extract
from fmpy.fmi2 import FMU2Slave
from fmpy.fmi3 import FMU3Slave
import collections

META = {
    "api_version": "3.0",
    "type": "hybrid",
    'models': {
        'V_source': {
            'public': True,
            'params': [],
            'attrs': ['V_source_mag', 'V_source_angle'],
        },
    },
}

class V_source(mosaik_api.Simulator):
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

        self.vr_inputs = [] # V_source has no inputs
        self.vr_outputs = [self.vrs['V_source_mag'], self.vrs['V_source_angle']]

        print(self.vrs)
        print("Initialization of FMU V_source successful")

        return self.meta
    
    def create(self, num, model):
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
                # Initialize FMU
                self._entities[eid].enterInitializationMode()
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
                fmu.exitInitializationMode()
                
                self.fmutimes[eid] = self.start_time*self.sec_per_mt

                entities.append( { 'eid': eid, 'type': model, 'rel': [] } )

        return entities
    
    def step(self, time, inputs, max_advance):
        target_time = (time + 1 + self.start_time) * self.sec_per_mt
        
        for eid in self._entities:
            
            communication_point = self.fmutimes[eid] 
            communication_step_size = target_time - communication_point

            status = self._entities[eid].doStep(currentCommunicationPoint = communication_point,
                    communicationStepSize = communication_step_size)
            
            self.fmutimes[eid] += communication_step_size

        return time + 1  # Return next Mosaik time step
    
    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            if eid in self._entities:
                data[eid] = {}
                for attr in attrs:

                    if self.model_description.fmiVersion == '2.0':
                        if attr == 'V_source_mag':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['V_source_mag']])[0]
                        elif attr == 'V_source_angle':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['V_source_angle']])[0]

                    elif self.model_description.fmiVersion == '3.0':
                        if attr == 'V_source_mag':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['V_source_mag']])[0]
                        elif attr == 'V_source_angle':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['V_source_angle']])[0]

        return data
