# Care Razorpay

[![Release Status](https://img.shields.io/pypi/v/care_razorpay.svg)](https://pypi.python.org/pypi/care_razorpay)
[![Build Status](https://github.com/ohcnetwork/care_razorpay/actions/workflows/build.yaml/badge.svg)](https://github.com/ohcnetwork/care_razorpay/actions/workflows/build.yaml)

Care Razorpay is a plugin for care to integrate Razorpay payment gateway.

## Local Development

To develop the plug in local environment along with care, follow the steps below:

1. Go to the care root directory and clone the plugin repository:

```bash
cd care
git clone git@github.com:ohcnetwork/care_razorpay.git
```

2. Add the plugin config in plug_config.py

```python
...

care_razorpay_plugin = Plug(
    name="care_razorpay", # name of the django app in the plugin
    package_name="/app/care_razorpay", # this has to be /app/ + plugin folder name
    version="", # keep it empty for local development
    configs={}, # plugin configurations if any
)
plugs = [care_razorpay_plugin]

...
```

3. Tweak the code in plugs/manager.py, install the plugin in editable mode

```python
...

subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "-e", *packages] # add -e flag to install in editable mode
)

...
```

4. Rebuild the docker image and run the server

```bash
make re-build
make up
```

> [!IMPORTANT]
> Do not push these changes in a PR. These changes are only for local development.

## Production Setup

To install care razorpay, you can add the plugin config in [care/plug_config.py](https://github.com/ohcnetwork/care/blob/develop/plug_config.py) as follows:

```python
...

care_razorpay_plugin = Plug(
    name="care_razorpay",
    package_name="git+https://github.com/ohcnetwork/care_razorpay.git",
    version="@master",
    configs={},
)
plugs = [care_razorpay_plugin]
...
```

[Extended Docs on Plug Installation](https://care-be-docs.ohc.network/pluggable-apps/configuration.html)

## Configuration

The following configurations variables are available for Care Razorpay:

- `RAZORPAY_KEY_ID`: Razorpay API key
- `RAZORPAY_KEY_SECRET`: Razorpay API secret
- `RAZORPAY_WEBHOOK_SECRET`: Razorpay webhook secret

The plugin will try to find the API key from the config first and then from the environment variable.

## License

This project is licensed under the terms of the [MIT license](LICENSE).

---

This plugin was created with [Cookiecutter](https://github.com/audreyr/cookiecutter) using the [ohcnetwork/care-plugin-cookiecutter](https://github.com/ohcnetwork/care-plugin-cookiecutter).
