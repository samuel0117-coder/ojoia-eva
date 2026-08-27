import torch
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
from PIL import Image
import sys

def main():
    print("Loading processor...")
    processor = AutoProcessor.from_pretrained(
        "/opt/ai_models/Qwen2.5-VL-7B-Instruct-AWQ",
        trust_remote_code=True
    )
    print("Loading model...")
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        "/opt/ai_models/Qwen2.5-VL-7B-Instruct-AWQ",
        trust_remote_code=True,
        torch_dtype=torch.float16,
        device_map="auto"
    )
    print("Model loaded successfully.")
    
    # Test text generation
    print("\n--- Text Generation Test ---")
    text_input = "Hello, how are you?"
    inputs = processor(text=text_input, return_tensors="pt").to(model.device)
    print(f"Input: {text_input}")
    generated_ids = model.generate(**inputs, max_new_tokens=50)
    generated_text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
    print(f"Output: {generated_text}")
    
    # Test image description
    print("\n--- Image Description Test ---")
    # Create a dummy red image
    dummy_image = Image.new('RGB', (224, 224), color='red')
    # You can also load a real image: dummy_image = Image.open("path/to/image.jpg")
    inputs = processor(images=dummy_image, text="Describe this image.", return_tensors="pt").to(model.device)
    print("Input: dummy red image, prompt: 'Describe this image.'")
    generated_ids = model.generate(**inputs, max_new_tokens=50)
    generated_text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
    print(f"Output: {generated_text}")

if __name__ == "__main__":
    main()
