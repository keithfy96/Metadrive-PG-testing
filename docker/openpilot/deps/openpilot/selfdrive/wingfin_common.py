from typing import Any
def get_inference_config():
  try:
    import sys
    sys.path.append('/opt/project/common')
    from inference_config import InferenceConfig
    sys.path.pop()
    return InferenceConfig()
  except Exception as e:
    print(f'eror loading InferenceConfig into OP {e}')
    return None


class InferenceConfigParser:
  def __init__(self):
    self.infConfig = get_inference_config()

  def get_value(self, val_name: str, default: Any) -> Any:
    if self.infConfig is None:
      print(f'eror loading InferenceConfig into OpenPilot, value is None !!!!!!!!')
      return default

    if hasattr( self.infConfig, val_name):
      value = getattr( self.infConfig, val_name)
      print(f"Loaded '{val_name}' from config: {self.infConfig}")
      return value
    else:
      print(f"Attribute '{val_name}' not found in config. Using default value: {default}")
      return default
