# Copyright (c) 2024 Alibaba Inc (authors: Xiang Lyu, Zhihao Du)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# antflake8: noqa
import os
import torch

try:
    import tensorrt as trt
except ImportError:
    import warnings
    warnings.warn("Failed to import TensorRT. Make sure TensorRT is installed and available in your environment.", ImportWarning)

import torch.nn.functional as F
from matcha.models.components.flow_matching import BASECFM

class ConditionalCFM(BASECFM):
    def __init__(self, in_channels, cfm_params, n_spks=1, spk_emb_dim=64, tensorrt_model_path="estimator_fp16.plan", estimator: torch.nn.Module = None):
        super().__init__(
            n_feats=in_channels,
            cfm_params=cfm_params,
            n_spks=n_spks,
            spk_emb_dim=spk_emb_dim,
        )
        self.t_scheduler = cfm_params.t_scheduler
        self.training_cfg_rate = cfm_params.training_cfg_rate
        self.inference_cfg_rate = cfm_params.inference_cfg_rate
        in_channels = in_channels + (spk_emb_dim if n_spks > 0 else 0)
        # Just change the architecture of the estimator here
        self.estimator = estimator
        self.compiled_estimator = None

        self.export_onnx = False
        self.use_tensorrt = False

        if os.path.isfile(tensorrt_model_path):
            trt.init_libnvinfer_plugins(None, "")
            logger = trt.Logger(trt.Logger.WARNING)
            runtime = trt.Runtime(logger)
            with open(tensorrt_model_path, 'rb') as f:
                serialized_engine = f.read()
            self.engine = runtime.deserialize_cuda_engine(serialized_engine)
            self._context = self.engine.create_execution_context()
            self.use_tensorrt = True

    @torch.inference_mode()
    def forward(self, mu, mask, n_timesteps, temperature=1.0, spks=None, cond=None):
        """Forward diffusion

        Args:
            mu (torch.Tensor): output of encoder
                shape: (batch_size, n_feats, mel_timesteps)
            mask (torch.Tensor): output_mask
                shape: (batch_size, 1, mel_timesteps)
            n_timesteps (int): number of diffusion steps
            temperature (float, optional): temperature for scaling noise. Defaults to 1.0.
            spks (torch.Tensor, optional): speaker ids. Defaults to None.
                shape: (batch_size, spk_emb_dim)
            cond: Not used but kept for future purposes

        Returns:
            sample: generated mel-spectrogram
                shape: (batch_size, n_feats, mel_timesteps)
        """
        z = torch.randn_like(mu) * temperature
        t_span = torch.linspace(0, 1, n_timesteps + 1, device=mu.device, dtype=mu.dtype)
        if self.t_scheduler == 'cosine':
            t_span = 1 - torch.cos(t_span * 0.5 * torch.pi)
        return self.solve_euler(z, t_span=t_span, mu=mu, mask=mask, spks=spks, cond=cond)

    def estimator_infer(self, x, mask, mu, t, spks, cond):
        if self.use_tensorrt:
            # print("Using tensorrt now !!!!")
            bs = x.shape[0]
            hs = x.shape[1]
            seq_len = x.shape[2]

            assert bs == 1 and hs == 80

            ret = torch.empty_like(x)
            self._context.set_input_shape("x", x.shape)
            self._context.set_input_shape("mask", mask.shape)
            self._context.set_input_shape("mu", mu.shape)
            self._context.set_input_shape("t", t.shape)
            self._context.set_input_shape("spks", spks.shape)
            self._context.set_input_shape("cond", cond.shape)

            bindings = [x.data_ptr(), mask.data_ptr(), mu.data_ptr(), t.data_ptr(), spks.data_ptr(), cond.data_ptr(), ret.data_ptr()]

            for i in range(len(bindings)):
                self._context.set_tensor_address(self.engine.get_tensor_name(i), bindings[i])

            handle = torch.cuda.current_stream().cuda_stream
            self._context.execute_async_v3(stream_handle=handle)
            return ret
        else:
            return self.estimator.forward(x, mask, mu, t, spks, cond)

    def solve_euler(self, x, t_span, mu, mask, spks, cond):
        """
        Fixed euler solver for ODEs.
        Args:
            x (torch.Tensor): random noise
            t_span (torch.Tensor): n_timesteps interpolated
                shape: (n_timesteps + 1,)
            mu (torch.Tensor): output of encoder
                shape: (batch_size, n_feats, mel_timesteps)
            mask (torch.Tensor): output_mask
                shape: (batch_size, 1, mel_timesteps)
            spks (torch.Tensor, optional): speaker ids. Defaults to None.
                shape: (batch_size, spk_emb_dim)
            cond: Not used but kept for future purposes
        """
        t, _, dt = t_span[0], t_span[-1], t_span[1] - t_span[0]
        t = t.unsqueeze(dim=0)

        # I am storing this because I can later plot it by putting a debugger here and saving it to a file
        # Or in future might add like a return_all_steps flag
        sol = []

        # self.export_onnx= True
        # if self.export_onnx == True:
        #     dummy_input = (x, mask, mu, t, spks, cond)
        #     torch.onnx.export(
        #         self.estimator,
        #         dummy_input,
        #         "estimator_bf16.onnx",
        #         export_params=True,
        #         opset_version=18,
        #         do_constant_folding=True,
        #         input_names=['x', 'mask', 'mu', 't', 'spks', 'cond'],
        #         output_names=['output'],
        #         dynamic_axes={
        #             'x': {2: 'seq_len'},
        #             'mask': {2: 'seq_len'},
        #             'mu': {2: 'seq_len'},
        #             'cond': {2: 'seq_len'},
        #             'output': {2: 'seq_len'},
        #         }
        #     )
        #     onnx_file_path = "estimator_bf16.onnx"
        #     tensorrt_path = "/root/TensorRT-10.2.0.19"
        #     if not tensorrt_path:
        #         raise EnvironmentError("Please set the 'tensorrt_root_dir' environment variable.")

        #     if not os.path.isdir(tensorrt_path):
        #         raise FileNotFoundError(f"The directory {tensorrt_path} does not exist.")

        #     trt_lib_path = os.path.join(tensorrt_path, "lib")
        #     if trt_lib_path not in os.environ.get('LD_LIBRARY_PATH', ''):
        #         print(f"Adding TensorRT lib path {trt_lib_path} to LD_LIBRARY_PATH.")
        #         os.environ['LD_LIBRARY_PATH'] = f"{os.environ.get('LD_LIBRARY_PATH', '')}:{trt_lib_path}"

        #     trt_file_name = 'estimator_bf16.plan'
        #     flow_model_dir ='.'
        #     # trt_file_path = os.path.join(flow_model_dir, trt_file_name)

        #     trtexec_bin = os.path.join(tensorrt_path, 'bin/trtexec')
        #     trtexec_cmd = f"{trtexec_bin} --onnx={onnx_file_path} --saveEngine={trt_file_name} " \
        #                 "--minShapes=x:1x80x1,mask:1x1x1,mu:1x80x1,t:1,spks:1x80,cond:1x80x1 " \
        #                 "--maxShapes=x:1x80x4096,mask:1x1x4096,mu:1x80x4096,t:1,spks:1x80,cond:1x80x4096 " + \
        #                 "--fp16"

        #     print("execute tensorrt", trtexec_cmd)
        #     os.system(trtexec_cmd)
        # #     """
        # #     ${TensorRT-10.2.0.19}/bin/trtexec --onnx=estimator_fp16.onnx --saveEngine=estimator_fp16.plan \
        # #         --minShapes=x:1x80x1,mask:1x1x1,mu:1x80x1,t:1,spks:1x80,cond:1x80x1 \
        # #         --maxShapes=x:1x80x4096,mask:1x1x4096,mu:1x80x4096,t:1,spks:1x80,cond:1x80x4096 \
        # #         --fp16 --verbose
        # #     """


        for step in range(1, len(t_span)):
            dphi_dt = self.estimator_infer(x, mask, mu, t, spks, cond).clone()
            # Classifier-Free Guidance inference introduced in VoiceBox
            if self.inference_cfg_rate > 0:
                cfg_dphi_dt = self.estimator_infer(
                    x, mask,
                    torch.zeros_like(mu), t,
                    torch.zeros_like(spks) if spks is not None else None,
                    torch.zeros_like(cond)
                ).clone()
                dphi_dt = ((1.0 + self.inference_cfg_rate) * dphi_dt - self.inference_cfg_rate * cfg_dphi_dt)
            x = x + dt * dphi_dt
            t = t + dt
            sol.append(x)
            if step < len(t_span) - 1:
                dt = t_span[step + 1] - t

        return sol[-1]

    def compute_loss(self, x1, mask, mu, spks=None, cond=None):
        """Computes diffusion loss

        Args:
            x1 (torch.Tensor): Target
                shape: (batch_size, n_feats, mel_timesteps)
            mask (torch.Tensor): target mask
                shape: (batch_size, 1, mel_timesteps)
            mu (torch.Tensor): output of encoder
                shape: (batch_size, n_feats, mel_timesteps)
            spks (torch.Tensor, optional): speaker embedding. Defaults to None.
                shape: (batch_size, spk_emb_dim)

        Returns:
            loss: conditional flow matching loss
            y: conditional flow
                shape: (batch_size, n_feats, mel_timesteps)
        """
        b, _, t = mu.shape

        # random timestep
        t = torch.rand([b, 1, 1], device=mu.device, dtype=mu.dtype)
        if self.t_scheduler == 'cosine':
            t = 1 - torch.cos(t * 0.5 * torch.pi)
        # sample noise p(x_0)
        z = torch.randn_like(x1)

        y = (1 - (1 - self.sigma_min) * t) * z + t * x1
        u = x1 - (1 - self.sigma_min) * z

        # during training, we randomly drop condition to trade off mode coverage and sample fidelity
        if self.training_cfg_rate > 0:
            cfg_mask = torch.rand(b, device=x1.device) > self.training_cfg_rate
            mu = mu * cfg_mask.view(-1, 1, 1)
            spks = spks * cfg_mask.view(-1, 1)
            cond = cond * cfg_mask.view(-1, 1, 1)

        pred = self.estimator(y, mask, mu, t.squeeze(), spks, cond)
        loss = F.mse_loss(pred * mask, u * mask, reduction="sum") / (torch.sum(mask) * u.shape[1])
        return loss, y
