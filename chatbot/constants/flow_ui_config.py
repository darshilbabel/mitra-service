import copy


DEFAULT_CONFIG = {
  "popup": {
    "allow_completion_escape": False,
    "show_completion_close_button": False,
    "allow_completion_outside_click": False,
    "completion_confirmation_image_url": "https://static-media.gritworks.ai/fe-images/PNG/Shikshalokam/shikshagrahaLogo.png",
    "show_completion_confirmation_button": True,
    "completion_confirmation_image_height": "70"
  },
  "header": {
    "new_chat_button": True
  }
}


def get_default_ui_config(config_type=None):
    if config_type is None:
        config = DEFAULT_CONFIG
    else:
        config = DEFAULT_CONFIG.get(config_type, {})

    return copy.deepcopy(config)
