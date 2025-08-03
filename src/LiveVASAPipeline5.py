# encoding = 'utf-8'
import os
import os.path as osp
import sys
from omegaconf import OmegaConf

import pickle
import subprocess

import cv2; cv2.setNumThreads(0); cv2.ocl.setUseOpenCL(False)

import torch
torch.backends.cudnn.benchmark = True # disable CUDNN_BACKEND_EXECUTION_PLAN_DESCRIPTOR warning

sys.path.append(osp.dirname(osp.dirname(osp.dirname(osp.dirname(osp.realpath(__file__))))))

from fvcore.nn import FlopCountAnalysis
from src.datasets.preprocess.extract_features.audio_processer import AudioProcessor
from src.datasets.preprocess.extract_features.motion_processer import MotionProcesser
from src.models.dit.talking_head_diffusion_v15 import MotionDiffusion
from src.datasets.data_utils import read_video_cv2, get_video_length

from src.utils.rprint import rlog as log
import time

emo_map = {
    0: 'Anger', 
    1: 'Contempt', 
    2: 'Disgust', 
    3: 'Fear', 
    4: 'Happiness', 
    5: 'Neutral', 
    6: 'Sadness', 
    7: 'Surprise',
    8: 'None'
}
# import torch
import random
import numpy as np

def set_seed(seed: int = 42):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # 如果使用多个 GPU
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False  # 关闭 CuDNN 优化以保证可复现性

# 在推理前调用
set_seed(42)

class NullableArgs:
    def __init__(self, namespace):
        for key, value in namespace.__dict__.items():
            setattr(self, key, value)


class LiveVASAPipeline(object):
    def __init__(self, cfg_path: str, load_motion_generator: bool = True, motion_mean_std_path=None):
        """The pipeline for LiveVASA
        The pipeline for LiveVASA

        Args:
            cfg_path (str): YAML config file path of LiveVASA
        """
        # pretrained encoders of live portrait
        cfg = OmegaConf.load(cfg_path)
        self.device_id = cfg.device_id
        self.device = f"cuda:{self.device_id}"
        
        # 1 load audio processor
        self.audio_processor: AudioProcessor = AudioProcessor(cfg_path=cfg.audio_model_config, is_training=False)
        log(f"Load audio_processor done.")

        # 2 load motion generator
        self.prev_length = cfg.prev_length
        self.pred_length = cfg.pred_length
        if cfg.motion_models_config is not None and load_motion_generator:
            motion_models_config = OmegaConf.load(cfg.motion_models_config)
            log(f"Load motion_models_config from {osp.realpath(cfg.motion_models_config)} done.")
            self.motion_generator = MotionDiffusion(motion_models_config, device=self.device)
            self.load_motion_generator(self.motion_generator, cfg.motion_generator_path)
            # self.motion_generator.eval()
        else:
            self.motion_generator = None    
            log(f"Init motion_generator as None.")
        
        # 3. load motion processer
        self.motion_processer: MotionProcesser = MotionProcesser(cfg_path=cfg.motion_processer_config, device_id=cfg.device_id)
        log(f"Load motion_processor done.")

        # 4. load motion template
        self.templete_dict = pickle.load(open(cfg.motion_template_path, 'rb'))

        self.motion_mean_std = None
        if motion_mean_std_path is not None:
            self.motion_mean_std = torch.load(motion_mean_std_path)
            self.motion_mean_std["mean"] = self.motion_mean_std["mean"].to(self.device)
            self.motion_mean_std["std"] = self.motion_mean_std["std"].to(self.device)
            print(f"scale mean: {self.motion_mean_std['mean'][0, 63:64]}, std: {self.motion_mean_std['std'][0, 63:64]}")
            print(f"t mean: {self.motion_mean_std['mean'][0, 64:67]}, std: {self.motion_mean_std['std'][0, 64:67]}")
            print(f"pitch mean: {self.motion_mean_std['mean'][0, 67:68]}, std: {self.motion_mean_std['std'][0, 67:68]}")
            print(f"yaw mean: {self.motion_mean_std['mean'][0, 68:69]}, std: {self.motion_mean_std['std'][0, 68:69]}")
            print(f"scoll mean: {self.motion_mean_std['mean'][0, 69:70]}, std: {self.motion_mean_std['std'][0, 69:70]}")

        self.cfg = cfg

    def set_motion_generator(self, motion_generator: MotionDiffusion):
        self.motion_generator = motion_generator
        self.motion_generator.to(self.device)
        
    def load_motion_generator(self, model, motion_generator_path: str):
        print(motion_generator_path)
        model_data = torch.load(motion_generator_path, map_location=self.device)
        model.load_state_dict(model_data, strict=False)
       

        model.to(self.device)
        model.eval()

    
    def get_motion_sequence(self, motion_data: torch.Tensor, rescale_ratio=1.0):
        n_frames = motion_data.shape[0]
        # denorm
        if self.motion_mean_std is not None:
            if motion_data.shape[1] > 70:
                motion_data[:, :63] = motion_data[:, :63] * (self.motion_mean_std["std"][:, :63] + 1e-5) + self.motion_mean_std["mean"][:, :63]
                # denorm pose
                motion_data[:, 63:] = motion_data[:, 63:] + self.motion_mean_std["mean"][:, 63:]
            else:
                motion_data = motion_data * (self.motion_mean_std["std"] + 1e-5) + self.motion_mean_std["mean"]

        kp_infos = {"exp": [], "scale": [], "t": [], "pitch": [], "yaw": [], "roll": []}
        for idx in range(n_frames):
            exp = motion_data[idx][:63]
            scale = motion_data[idx][63:64] * rescale_ratio
            t = motion_data[idx][64:67] * rescale_ratio
            if motion_data.shape[1] > 70:
                pitch = motion_data[idx][67:133]
                yaw = motion_data[idx][133:199]
                roll = motion_data[idx][199:265]
            else:
                pitch = motion_data[idx][67:68]
                yaw = motion_data[idx][68:69]
                roll = motion_data[idx][69:70]

            kp_infos["exp"].append(exp)
            kp_infos["scale"].append(scale)
            kp_infos["t"].append(t)
            kp_infos["pitch"].append(pitch)
            kp_infos["yaw"].append(yaw)
            kp_infos["roll"].append(roll)

        for k, v in kp_infos.items():
            kp_infos[k] = torch.stack(v)

        return kp_infos
    
    def get_prev_motion(self, x_s_info):
        kp_infos = []
        x_s_info["t"][:, 2] = 0  # zero tz
        if self.motion_generator is not None and self.motion_generator.input_dim == 70:
            x_s_info = self.motion_processer.refine_kp(x_s_info)
            for k, v in x_s_info.items():
                x_s_info[k] = v.reshape(1, -1)

        rescale_ratio = 1.0 if self.motion_mean_std is None else (x_s_info["scale"] + 1e-5) / (self.motion_mean_std["mean"][:, 63:64] + 1e-5)

        for feat_name in ["exp", "scale", "t", "pitch", "yaw", "roll"]:
            if feat_name in ["scale", "t"]:
                # set scale as the mean scale
                kp_infos.append(x_s_info[feat_name] / rescale_ratio)
            else:
                kp_infos.append(x_s_info[feat_name])
        kp_infos = torch.cat(kp_infos, dim=-1)   # B, D
        
        # normalize
        if self.motion_mean_std is not None:
            # normalize exp
            if self.motion_generator is not None and self.motion_generator.input_dim > 70:
                kp_infos[:, :63] = (kp_infos[:, :63] - self.motion_mean_std["mean"][:, :63]) / (self.motion_mean_std["std"][:, :63] + 1e-5)
                # normalize pose
                kp_infos[:, 63:] = kp_infos[:, 63:] - self.motion_mean_std["mean"][:, 63:]
            else:
                kp_infos = (kp_infos - self.motion_mean_std["mean"]) / (self.motion_mean_std["std"] + 1e-5)

        kp_infos = kp_infos.unsqueeze(1)    # B, D
        return kp_infos, rescale_ratio

    def process_audio(self, audio_path: str, mode="post"):
        # add silent audio to pad short input
        ori_audio_path = audio_path
        audio_path, add_frames = self.audio_processor.add_silent_audio(audio_path, linear_fusion=False, mode=mode)
        audio_emb = self.audio_processor.get_long_audio_emb(audio_path)
        return audio_emb, audio_path, add_frames, ori_audio_path

    def driven_sample(self, image_path: str, audio_path: str, cfg_scale: float=1., emo: int=8, save_dir=None, smooth=False, silent_audio_path = None, silent_mode="post"):
        assert self.motion_generator is not None, f"Motion Generator is not set"
        reference_name = osp.basename(image_path).split('.')[0]
        audio_name = osp.basename(audio_path).split('.')[0]
        start_time = time.time()
        # get audio embeddings
        audio_emb, audio_path, add_frames, ori_audio_path = self.process_audio(audio_path)

        # get src image infos
        source_rgb_lst = self.motion_processer.read_image(image_path)
        src_img_256x256, s_lmk, crop_info = self.motion_processer.crop_image(source_rgb_lst[0], do_crop=False)
        f_s, x_s_info = self.motion_processer.prepare_source(src_img_256x256)
        prev_motion, rescale_ratio = self.get_prev_motion(x_s_info)
        start_time1 = time.time()
        print("preprocess time",start_time1-start_time)
        # generate motions
        start_time2=time.time()
        motion = self.motion_generator.sample(audio_emb, x_s_info["kp"], prev_motion=prev_motion, cfg_scale=cfg_scale, emo=emo)
        print("denoise time",time.time()-start_time2)

        print(f"length of motion: {len(motion)}")
        kp_infos = self.get_motion_sequence(motion, rescale_ratio=rescale_ratio)
        
        # driven results
        if save_dir is None:
            save_dir = self.cfg.output_dir
        if not osp.exists(save_dir):
            os.makedirs(save_dir)
        #save_path = osp.join(save_dir, f'{reference_name}_{audio_name}_cfg-{cfg_scale}_emo-{emo_map[emo]}.mp4')
        save_path = osp.join(save_dir, f'{reference_name}.mp4')

        self.motion_processer.driven_by_audio(source_rgb_lst[0], kp_infos, save_path, ori_audio_path, smooth=smooth)
        print("fine time",time.time()-start_time)
        return save_path


    
    def viz_motion(self, motion_data):
        pass        
        
    def __call__(self):
        pass


if __name__ == "__main__":
    import time
    import random
    
    task = "test"

    cfg_path = " "
    crop_cfg_path = ""
    
    image_path = ''
    audio_path = ""
    motion_path = ""


    save_dir = ""

    cfg_scale = 1.2
    for emo in range(0,1):
        # emo =  5   #random.randint(0, 7
        smooth=False
        emo=0
        motion_mean_std_path = "celebv_text_v3_mead_v3_v2.pt"
        pipeline = LiveVASAPipeline(cfg_path=cfg_path, motion_mean_std_path=motion_mean_std_path)

        if not osp.exists(save_dir):
            os.makedirs(save_dir)

        save_dir = osp.join(save_dir, f"cfg-{cfg_scale}-emo-{emo_map[emo]}")
        if not osp.exists(save_dir):
            os.makedirs(save_dir)

        if task == "test":
            # testing
            file_list = "/file_list.txt"

            with open(file_list, "r") as f:
                lines = f.readlines()
                image_paths = [line.strip().split("\t")[0] for line in lines]
                audio_paths = [line.strip().split("\t")[-1] for line in lines]
                emos = [line.strip().split("\t")[-2] for line in lines]

            tic = time.time()
            idx = 0
            total_len = len(image_paths)
            for image_path, audio_path in zip(image_paths, audio_paths):
                idx += 1
                print(f"[{idx}/{total_len}] Processing...")
                
                video_path = pipeline.driven_sample(
                    image_path, audio_path, 
                    cfg_scale=cfg_scale, emo=8, 
                    save_dir=save_dir, smooth=smooth,
                )
                print(f"Video Result has been saved into: {video_path}")
            print(f"Testing moda Done. Costs {time.time() - tic:.2f}s")


