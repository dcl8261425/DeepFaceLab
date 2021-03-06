import os
import time
import inspect
import operator
import pickle
from pathlib import Path
from utils import Path_utils
from utils import std_utils
from utils import image_utils
import numpy as np
import cv2
import gpufmkmgr
from samples import SampleGeneratorBase

'''
You can implement your own model. Check examples.
'''
class ModelBase(object):

    #DONT OVERRIDE
    def __init__(self, model_path, training_data_src_path=None, training_data_dst_path=None,
                        batch_size=0,
                        write_preview_history = False,
                        debug = False, **in_options
                ):
        print ("Loading model...")
        self.model_path = model_path
        self.model_data_path = Path( self.get_strpath_storage_for_file('data.dat') )
        
        self.training_data_src_path = training_data_src_path
        self.training_data_dst_path = training_data_dst_path
        
        self.src_images_paths = None
        self.dst_images_paths = None
        self.src_yaw_images_paths = None
        self.dst_yaw_images_paths = None
        self.src_data_generator = None
        self.dst_data_generator = None
        self.is_training_mode = (training_data_src_path is not None and training_data_dst_path is not None)
        self.batch_size = batch_size
        self.write_preview_history = write_preview_history
        self.debug = debug
        self.supress_std_once = ('TF_SUPPRESS_STD' in os.environ.keys() and os.environ['TF_SUPPRESS_STD'] == '1')
        
        if self.model_data_path.exists():            
            model_data = pickle.loads ( self.model_data_path.read_bytes() )            
            self.epoch = model_data['epoch']            
            self.options = model_data['options']
            self.loss_history = model_data['loss_history'] if 'loss_history' in model_data.keys() else []
            self.sample_for_preview = model_data['sample_for_preview']  if 'sample_for_preview' in model_data.keys() else None
        else:
            self.epoch = 0
            self.options = {}
            self.loss_history = []
            self.sample_for_preview = None
            
        if self.write_preview_history:
            self.preview_history_path = self.model_path / ( '%s_history' % (self.get_model_name()) )
            
            if not self.preview_history_path.exists():
                self.preview_history_path.mkdir(exist_ok=True)
            else:
                if self.epoch == 0:
                    for filename in Path_utils.get_image_paths(self.preview_history_path):
                        Path(filename).unlink()    

                        
        self.gpu_config = gpufmkmgr.GPUConfig(allow_growth=False, **in_options)
        self.gpu_total_vram_gb = self.gpu_config.gpu_total_vram_gb

        if self.epoch == 0: 
            #first run         
            self.options['created_vram_gb'] = self.gpu_total_vram_gb
            self.created_vram_gb = self.gpu_total_vram_gb
        else: 
            #not first run        
            if 'created_vram_gb' in self.options.keys():
                self.created_vram_gb = self.options['created_vram_gb']
            else:
                self.options['created_vram_gb'] = self.gpu_total_vram_gb
                self.created_vram_gb = self.gpu_total_vram_gb
            
        self.tf = gpufmkmgr.import_tf( self.gpu_config )
        self.tf_sess = gpufmkmgr.get_tf_session()
        self.keras = gpufmkmgr.import_keras()
        self.keras_contrib = gpufmkmgr.import_keras_contrib()

        self.onInitialize(**in_options)
        
        if self.debug or self.batch_size == 0:
            self.batch_size = 1 
        
        if self.is_training_mode:
            if self.generator_list is None:
                raise Exception( 'You didnt set_training_data_generators()')
            else:
                for i, generator in enumerate(self.generator_list):
                    if not isinstance(generator, SampleGeneratorBase):
                        raise Exception('training data generator is not subclass of SampleGeneratorBase')
                        
            if self.sample_for_preview is None:
                self.sample_for_preview = self.generate_next_sample()

        print ("===== Model summary =====")
        print ("== Model name: " + self.get_model_name())
        print ("==")
        print ("== Current epoch: " + str(self.epoch) )
        print ("==")
        print ("== Options:")
        print ("== |== batch_size : %s " % (self.batch_size) )
        print ("== |== multi_gpu : %s " % (self.gpu_config.multi_gpu) )
        for key in self.options.keys():
            print ("== |== %s : %s" % (key, self.options[key]) )        
        
        print ("== Running on:")
        if self.gpu_config.cpu_only:
            print ("== |== [CPU]")
        else:
            for idx in self.gpu_config.gpu_idxs:
                print ("== |== [%d : %s]" % (idx, gpufmkmgr.getDeviceName(idx)) )
 
        if not self.gpu_config.cpu_only and self.gpu_total_vram_gb == 2:
            print ("==")
            print ("== WARNING: You are using 2GB GPU. Result quality may be significantly decreased.")
            print ("== If training does not start, close all programs and try again.")
            print ("== Also you can disable Windows Aero Desktop to get extra free VRAM.")
            print ("==")
            
        print ("=========================")
  
    #overridable
    def onInitialize(self, **in_options):
        '''
        initialize your keras models
        
        store and retrieve your model options in self.options['']
        
        check example
        '''
        pass
        
    #overridable
    def onSave(self):
        #save your keras models here
        pass

    #overridable
    def onTrainOneEpoch(self, sample):
        #train your keras models here

        #return array of losses
        return ( ('loss_src', 0), ('loss_dst', 0) )

    #overridable
    def onGetPreview(self, sample):
        #you can return multiple previews
        #return [ ('preview_name',preview_rgb), ... ]        
        return []

    #overridable if you want model name differs from folder name
    def get_model_name(self):
        return Path(inspect.getmodule(self).__file__).parent.name.rsplit("_", 1)[1]
        
    #overridable
    def get_converter(self, **in_options):
        #return existing or your own converter which derived from base        
        from .ConverterBase import ConverterBase
        return ConverterBase(self, **in_options) 
     
    def to_multi_gpu_model_if_possible (self, models_list):
        if len(self.gpu_config.gpu_idxs) > 1:
            #make batch_size to divide on GPU count without remainder
            self.batch_size = int( self.batch_size / len(self.gpu_config.gpu_idxs) )
            if self.batch_size == 0:
                self.batch_size = 1                
            self.batch_size *= len(self.gpu_config.gpu_idxs)
            
            result = []
            for model in models_list:
                for i in range( len(model.output_names) ):
                    model.output_names = 'output_%d' % (i)                 
                result += [ self.keras.utils.multi_gpu_model( model, self.gpu_config.gpu_idxs ) ]    
                
            return result                
        else:
            return models_list
     
    def get_previews(self):       
        return self.onGetPreview ( self.last_sample )
        
    def get_static_preview(self):        
        return self.onGetPreview (self.sample_for_preview)[0][1] #first preview, and bgr
       
    def save(self):    
        print ("Saving...")
        
        if self.supress_std_once:
            supressor = std_utils.suppress_stdout_stderr()
            supressor.__enter__()
            
        self.onSave()
        
        if self.supress_std_once:
            supressor.__exit__()
        
        model_data = {
            'epoch': self.epoch,
            'options': self.options,
            'loss_history': self.loss_history,
            'sample_for_preview' : self.sample_for_preview
        }            
        self.model_data_path.write_bytes( pickle.dumps(model_data) )

    def save_weights_safe(self, model_filename_list):
        for model, filename in model_filename_list:
            model.save_weights( filename + '.tmp' )
            
        for model, filename in model_filename_list:
            source_filename = Path(filename+'.tmp')
            target_filename = Path(filename)
            if target_filename.exists():
                target_filename.unlink()
                
            source_filename.rename ( str(target_filename) )
        
    def debug_one_epoch(self):
        images = []
        for generator in self.generator_list:        
            for i,batch in enumerate(next(generator)):
                images.append( batch[0] )
        
        return image_utils.equalize_and_stack_square (images)
        
    def generate_next_sample(self):
        return [next(generator) for generator in self.generator_list]

    def train_one_epoch(self):    
        if self.supress_std_once:
            supressor = std_utils.suppress_stdout_stderr()
            supressor.__enter__()
            
        self.last_sample = self.generate_next_sample() 

        epoch_time = time.time()
        
        losses = self.onTrainOneEpoch(self.last_sample)
        
        epoch_time = time.time() - epoch_time

        self.loss_history.append ( [float(loss[1]) for loss in losses] )
        
        if self.supress_std_once:
            supressor.__exit__()
            self.supress_std_once = False
                  
        if self.write_preview_history:
            if self.epoch % 10 == 0:
                img = (self.get_static_preview() * 255).astype(np.uint8)
                cv2.imwrite ( str (self.preview_history_path / ('%.6d.jpg' %( self.epoch) )), img )     
                
        self.epoch += 1
        
        #............."Saving... 
        if epoch_time >= 10000:
            loss_string = "Training [#{0:06d}][{1:03d}s]".format ( self.epoch, epoch_time / 1000 )
        else:
            loss_string = "Training [#{0:06d}][{1:04d}ms]".format ( self.epoch, int(epoch_time*1000) % 10000 )
        for (loss_name, loss_value) in losses:
            loss_string += " %s:%.3f" % (loss_name, loss_value)

        return loss_string
        
    def pass_one_epoch(self):
        self.last_sample = self.generate_next_sample()     
        
    def finalize(self):
        gpufmkmgr.finalize_keras()
                
    def is_first_run(self):
        return self.epoch == 0
        
    def is_debug(self):
        return self.debug
        
    def get_epoch(self):
        return self.epoch
        
    def get_loss_history(self):
        return self.loss_history
 
    def set_training_data_generators (self, generator_list):
        self.generator_list = generator_list
        
    def get_training_data_generators (self):
        return self.generator_list
        
    def get_strpath_storage_for_file(self, filename):
        return str( self.model_path / (self.get_model_name() + '_' + filename) )

    def set_vram_batch_requirements (self, d):
        #example d = {2:2,3:4,4:8,5:16,6:32,7:32,8:32,9:48} 
        keys = [x for x in d.keys()]
        
        if self.gpu_config.cpu_only:
            if self.batch_size == 0:
                self.batch_size = 2
        else:
            if self.gpu_total_vram_gb < keys[0]:
                raise Exception ('Sorry, this model works only on %dGB+ GPU' % ( keys[0] ) )

            if self.batch_size == 0:        
                for x in keys:
                    if self.gpu_total_vram_gb <= x:
                        self.batch_size = d[x]
                        break
                        
                if self.batch_size == 0:
                    self.batch_size = d[ keys[-1] ]