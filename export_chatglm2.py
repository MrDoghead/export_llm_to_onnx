import os
import argparse
import torch
from torch import nn
from transformers import AutoTokenizer, AutoModel
 
 
def export_output_layer(output_layer, config, dtype, args, model_name):
    # fake size used to generate fake data
    batch = 1
    seq = 1
    hidden_size = config.hidden_size
 
    input_shape = [batch, seq, hidden_size]
    input_data = torch.randn(input_shape, dtype=dtype).to(args.device)
    onnx_file_name = os.path.join(args.out_dir, f"{model_name}.onnx")
 
    # Export the model
    torch.onnx.export(
        output_layer,
        input_data,
        onnx_file_name,
        opset_version=args.opset,
        do_constant_folding=True,
        input_names=['input'],
        output_names=['output'],
        dynamic_axes={
            'input': {1: 'N'}
        }
    )
 
 
def export_embeding(embed_model, config, args, model_name):
    batch = 1
    seq = 1
    input_shape = [batch, seq]
    dtype = torch.int64
    input_data = torch.ones(input_shape, dtype=dtype).to(args.device)
 
    onnx_file_name = os.path.join(args.out_dir, f"{model_name}.onnx")
 
    # Export the model
    torch.onnx.export(
        embed_model,
        input_data,
        onnx_file_name,
        opset_version=args.opset,
        do_constant_folding=True,
        input_names=['input'],
        output_names=['output'],
        dynamic_axes={
            'input': {1: 'N'}
        },
    )
 
 
class EncoderLayersWrapper(nn.Module):
    def __init__(self, chat_glm_model, config):
        super().__init__()
        self.chat_glm_model = chat_glm_model
        self.config = config
        self.max_seq_len = config.seq_length
        self.layer_num = chat_glm_model.encoder.num_layers
 
    def forward(
        self, inputs_embeds, attention_mask, position_ids, kv_caches
    ):
 
        # Rotary positional embeddings
        rotary_pos_emb = self.chat_glm_model.rotary_pos_emb(self.max_seq_len)
        rotary_pos_emb = rotary_pos_emb[position_ids]
        rotary_pos_emb = rotary_pos_emb.transpose(0, 1).contiguous()
 
        # list to [(past_key, past_value) x layers]
        past_key_values = []
        for i in range(self.layer_num):
            past_key_values.append((kv_caches[2*i], kv_caches[2*i+1]))
 
        # Run encoder.
        hidden_states, presents, all_hidden_states, all_self_attentions = self.chat_glm_model.encoder(
            inputs_embeds, attention_mask, rotary_pos_emb=rotary_pos_emb,
            kv_caches=past_key_values, use_cache=True, output_hidden_states=False
        )
 
        kv_caches_out = []
        for layer_cache in presents:
            kv_caches_out.extend(list(layer_cache))
 
        return hidden_states, *kv_caches_out
 
 
def export_encoders(chat_glm_model, config, dtype, args, model_name):
    """
    Note
    # please be care of the format of kv cache
    # some models use format of [batch, head, seq_len, hidden_size]
    # while some models use format of [batch, seq_len, head, hidden_size]
    """
    onnx_file_name = os.path.join(args.out_dir, f"{model_name}.onnx")
    encoder_layers_wrapper = EncoderLayersWrapper(chat_glm_model, config)
 
    hidden_size = config.hidden_size
    layer_num = chat_glm_model.encoder.num_layers
    print("layer_num:", layer_num)
 
    kv_channels = config.kv_channels
 
    batch = 1
    N = 1
    sumN = 32
    lastN = sumN - N
 
    hidden_in = torch.randn([N, batch, hidden_size], dtype=dtype).to(args.device)
    attention_mask = torch.zeros([batch, 1, N, sumN], dtype=torch.bool).to(args.device)
    position_ids = torch.ones([batch, N], dtype=torch.int64).to(args.device)
 
    in_names = ["hidden_in", "attention_mask", "position_ids"]
 
    dynamic_axes = {
        'hidden_in': {0: 'N', },
        'attention_mask': {2: 'N', 3: "sumN"},
        "position_ids": {1: 'N'},
    }
 
    kv_caches_in = []
    out_names = ["hidden_out"]
 
    kv_cache_in_shape = [lastN, 1, 2, kv_channels]
    kv_cache_dyn_axes = {0: "lastSum"}
 
    for i in range(layer_num):
        past_key_in = torch.randn(kv_cache_in_shape, dtype=dtype).to(args.device)
        past_value_in = torch.randn(kv_cache_in_shape, dtype=dtype).to(args.device)
 
        kv_caches_in.extend([past_key_in, past_value_in])
        in_names.extend([f"past_key_in{i}", f"past_value_in{i}"])
        out_names.extend([f"past_key{i}", f"past_value{i}"])
 
        dynamic_axes[f"past_key_in{i}"] = kv_cache_dyn_axes
        dynamic_axes[f"past_value_in{i}"] = kv_cache_dyn_axes
 
    input_datas = (hidden_in, attention_mask, position_ids, kv_caches_in)
 
    torch.onnx.export(
        encoder_layers_wrapper,
        input_datas,
        onnx_file_name,
        opset_version=args.opset,
        do_constant_folding=True,
        input_names=in_names,
        output_names=out_names,
        dynamic_axes=dynamic_axes,
    )
 
 
def export_chatglm2(args):
    device = args.device
    dtypes_config = {
        "fp32": False,
        "fp16": False,
        "bf16": False,
    }
    if args.dtype == "float32":
        dtype = torch.float32
    elif args.dtype == "float16":
        dtype = torch.float16
    elif args.dtype == "bfloat16":
        dtype = torch.bfloat16
 
    print(f"begin load model from {args.model_path}")
    # tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    model = AutoModel.from_pretrained(args.model_path, trust_remote_code=True).half()
    if args.dtype == "float32":
        model.float()
        print("convert model to float")
 
    if args.device == "cuda":
        model.cuda()
        print("convert model to cuda")
 
    model = model.eval()
 
    print(f"finish load model from {args.model_path}")
    config = model.config
 
    print("begin export output_layer")
    output_layer = model.transformer.output_layer
    export_output_layer(output_layer, config, dtype, args, "output_layer")
 
    print("begin export embeding_model")
    embeding_model = model.transformer.embedding
    export_embeding(embeding_model, config, args, "embeding")
 
    print("begin export transformer")
    chat_glm_model = model.transformer
    # chat_glm_model.encoder.num_layers = 1 # help debug
    export_encoders(chat_glm_model, config, dtype, args, "encoder_layers0")
 
 
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='export chatglm2',
    )
    parser.add_argument('-m', '--model_path', required=True, type=str)
    parser.add_argument('-o', '--out_dir', required=False, type=str, default="")
    parser.add_argument('--opset', required=False, type=int, default=15)
    parser.add_argument('-d', '--device', required=False, type=str, default="cuda")
    # supported dtype: ["float32", "float16", "bfloat16"]
    parser.add_argument('-p', '--dtype', required=False, type=str, default="float16")
    # 0: export all decoders into one onnx. >0: export multiple onnx files, and each onnx has decoder_pack_size layers
    parser.add_argument('--decoder_pack_size', required=False, type=int, default=0)
 
    args = parser.parse_args()
 
    if args.dtype not in ["float32", "float16", "bfloat16"]:
        raise ValueError("dtype is invalid")
 
    export_chatglm2(args)