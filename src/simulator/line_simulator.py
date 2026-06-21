import mosaik_api
import math
from itertools import count
from fmpy import read_model_description, extract
from fmpy.fmi2 import FMU2Slave
from fmpy.fmi3 import FMU3Slave
import collections

META = {
    "api_version": "3.0",
    "type": "hybrid",
    'models': {
        'Line': {
            'public': True,
            'params': ['r_ohm', 'x_ohm', 'bch'],
            'attrs': ['V_from_mag_kv', 'V_from_ang_deg', 'V_to_mag_kv', 'V_to_ang_deg',
                      'I_from_mag_kA', 'I_from_ang_deg', 'I_to_mag_kA', 'I_to_ang_deg',
                      'I_from_re', 'I_from_im', 'I_neg_from_re', 'I_neg_from_im',
                      'I_to_re', 'I_to_im',
                      'P_loss_mw', 'Q_loss_mvar'],
        },
    },
}

class Line(mosaik_api.Simulator):
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

        self.vr_inputs = [self.vrs['V_from_mag_kv'], self.vrs['V_from_ang_deg'], self.vrs['V_to_mag_kv'], self.vrs['V_to_ang_deg']]  # Line input variables
        self.vr_outputs = [self.vrs['I_from_mag_kA'], self.vrs['I_from_ang_deg'], self.vrs['I_to_mag_kA'], self.vrs['I_to_ang_deg'], self.vrs['P_loss_mw'], self.vrs['Q_loss_mvar']]  # Line output variables

        print(self.vrs)
        print("Initialization of FMU Line successful")

        return self.meta
    
    def create(self, num, model, r_ohm=1.0, x_ohm=1.0, bch=0.0):
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
                self._entities[eid].enterInitializationMode()
                # Set fixed parameters during initialization
                self._entities[eid].setReal([self.vrs['r_ohm']], [r_ohm])
                self._entities[eid].setReal([self.vrs['x_ohm']], [x_ohm])
                self._entities[eid].setReal([self.vrs['bch']], [bch])
                self._entities[eid].exitInitializationMode()

                self.fmutimes[eid] = self.start_time*self.sec_per_mt

                entities.append( { 'eid': eid, 'type': model, 'rel': [] } )

            elif self.model_description.fmiVersion == '3.0':
                fmu = FMU3Slave(
                guid=self.model_description.guid,
                unzipDirectory=self.unzipdir,
                modelIdentifier=self.model_description.coSimulation.modelIdentifier,
                instanceName=unique_instance_name)

                self._entities[eid] = fmu
             
                fmu.instantiate()
                fmu.enterInitializationMode(startTime=self.start_time)
                # Set fixed parameters during initialization
                fmu.setFloat64([self.vrs['r_ohm']], [r_ohm])
                fmu.setFloat64([self.vrs['x_ohm']], [x_ohm])
                fmu.setFloat64([self.vrs['bch']], [bch])
                fmu.exitInitializationMode()
                
                self.fmutimes[eid] = self.start_time*self.sec_per_mt

                entities.append( { 'eid': eid, 'type': model, 'rel': [] } )
           
        return entities
    
    def step(self, time, inputs, max_advance):
        target_time = (time + 1 + self.start_time) * self.sec_per_mt
        
        for eid in self._entities.keys():
            # Get input values for this entity
            if eid in inputs:
                entity_inputs = inputs[eid]

                if self.model_description.fmiVersion == '2.0':
                    # Set V_from_mag_kv input
                    if 'V_from_mag_kv' in entity_inputs:
                        v_from_mag_kv_values = entity_inputs['V_from_mag_kv']
                        if v_from_mag_kv_values:
                            v_from_mag_kv = sum(v_from_mag_kv_values.values())
                            self._entities[eid].setReal([self.vrs['V_from_mag_kv']], [v_from_mag_kv])

                    # Set V_from_ang_deg input
                    if 'V_from_ang_deg' in entity_inputs:
                        v_from_ang_deg_values = entity_inputs['V_from_ang_deg']
                        if v_from_ang_deg_values:
                            v_from_ang_deg = sum(v_from_ang_deg_values.values())
                            self._entities[eid].setReal([self.vrs['V_from_ang_deg']], [v_from_ang_deg])

                    # Set V_to_mag_kv input
                    if 'V_to_mag_kv' in entity_inputs:
                        v_to_mag_kv_values = entity_inputs['V_to_mag_kv']
                        if v_to_mag_kv_values:
                            v_to_mag_kv = sum(v_to_mag_kv_values.values())
                            self._entities[eid].setReal([self.vrs['V_to_mag_kv']], [v_to_mag_kv])

                    # Set V_to_ang_deg input
                    if 'V_to_ang_deg' in entity_inputs:
                        v_to_ang_deg_values = entity_inputs['V_to_ang_deg']
                        if v_to_ang_deg_values:
                            v_to_ang_deg = sum(v_to_ang_deg_values.values())
                            self._entities[eid].setReal([self.vrs['V_to_ang_deg']], [v_to_ang_deg])

                elif self.model_description.fmiVersion == '3.0':
                    # Set V_from_mag_kv input
                    if 'V_from_mag_kv' in entity_inputs:
                        v_from_mag_kv_values = entity_inputs['V_from_mag_kv']
                        if v_from_mag_kv_values:
                            v_from_mag_kv = sum(v_from_mag_kv_values.values())
                            self._entities[eid].setFloat64([self.vrs['V_from_mag_kv']], [v_from_mag_kv])

                    # Set V_from_ang_deg input
                    if 'V_from_ang_deg' in entity_inputs:
                        v_from_ang_deg_values = entity_inputs['V_from_ang_deg']
                        if v_from_ang_deg_values:
                            v_from_ang_deg = sum(v_from_ang_deg_values.values())
                            self._entities[eid].setFloat64([self.vrs['V_from_ang_deg']], [v_from_ang_deg])

                    # Set V_to_mag_kv input
                    if 'V_to_mag_kv' in entity_inputs:
                        v_to_mag_kv_values = entity_inputs['V_to_mag_kv']
                        if v_to_mag_kv_values:
                            v_to_mag_kv = sum(v_to_mag_kv_values.values())
                            self._entities[eid].setFloat64([self.vrs['V_to_mag_kv']], [v_to_mag_kv])

                    # Set V_to_ang_deg input
                    if 'V_to_ang_deg' in entity_inputs:
                        v_to_ang_deg_values = entity_inputs['V_to_ang_deg']
                        if v_to_ang_deg_values:
                            v_to_ang_deg = sum(v_to_ang_deg_values.values())
                            self._entities[eid].setFloat64([self.vrs['V_to_ang_deg']], [v_to_ang_deg])

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
                        if attr == 'I_from_mag_kA':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['I_from_mag_kA']])[0]
                        elif attr == 'I_from_ang_deg':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['I_from_ang_deg']])[0]
                        elif attr in ('I_from_re', 'I_from_im', 'I_neg_from_re', 'I_neg_from_im'):
                            mag = self._entities[eid].getReal([self.vrs['I_from_mag_kA']])[0]
                            ang = self._entities[eid].getReal([self.vrs['I_from_ang_deg']])[0]
                            re = mag * math.cos(math.radians(ang))
                            im = mag * math.sin(math.radians(ang))
                            data[eid]['I_from_re']     = re
                            data[eid]['I_from_im']     = im
                            data[eid]['I_neg_from_re'] = -re
                            data[eid]['I_neg_from_im'] = -im
                        elif attr == 'I_to_mag_kA':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['I_to_mag_kA']])[0]
                        elif attr == 'I_to_ang_deg':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['I_to_ang_deg']])[0]
                        elif attr in ('I_to_re', 'I_to_im'):
                            mag = self._entities[eid].getReal([self.vrs['I_to_mag_kA']])[0]
                            ang = self._entities[eid].getReal([self.vrs['I_to_ang_deg']])[0]
                            data[eid]['I_to_re'] = mag * math.cos(math.radians(ang))
                            data[eid]['I_to_im'] = mag * math.sin(math.radians(ang))
                        elif attr == 'P_loss_mw':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['P_loss_mw']])[0]
                        elif attr == 'Q_loss_mvar':
                            data[eid][attr] = self._entities[eid].getReal([self.vrs['Q_loss_mvar']])[0]
                    elif self.model_description.fmiVersion == '3.0':
                        if attr == 'I_from_mag_kA':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['I_from_mag_kA']])[0]
                        elif attr == 'I_from_ang_deg':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['I_from_ang_deg']])[0]
                        elif attr in ('I_from_re', 'I_from_im', 'I_neg_from_re', 'I_neg_from_im'):
                            mag = self._entities[eid].getFloat64([self.vrs['I_from_mag_kA']])[0]
                            ang = self._entities[eid].getFloat64([self.vrs['I_from_ang_deg']])[0]
                            re = mag * math.cos(math.radians(ang))
                            im = mag * math.sin(math.radians(ang))
                            data[eid]['I_from_re']     = re
                            data[eid]['I_from_im']     = im
                            data[eid]['I_neg_from_re'] = -re
                            data[eid]['I_neg_from_im'] = -im
                        elif attr == 'I_to_mag_kA':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['I_to_mag_kA']])[0]
                        elif attr == 'I_to_ang_deg':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['I_to_ang_deg']])[0]
                        elif attr in ('I_to_re', 'I_to_im'):
                            mag = self._entities[eid].getFloat64([self.vrs['I_to_mag_kA']])[0]
                            ang = self._entities[eid].getFloat64([self.vrs['I_to_ang_deg']])[0]
                            data[eid]['I_to_re'] = mag * math.cos(math.radians(ang))
                            data[eid]['I_to_im'] = mag * math.sin(math.radians(ang))
                        elif attr == 'P_loss_mw':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['P_loss_mw']])[0]
                        elif attr == 'Q_loss_mvar':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['Q_loss_mvar']])[0]
                        elif attr == 'P_loss_mw':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['P_loss_mw']])[0]
                        elif attr == 'Q_loss_mvar':
                            data[eid][attr] = self._entities[eid].getFloat64([self.vrs['Q_loss_mvar']])[0]
        return data
