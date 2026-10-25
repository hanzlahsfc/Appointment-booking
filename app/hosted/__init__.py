"""Hosted-client layer: provisioned accounts driving isolated portal browsers.

The hosted server (`python -m app.hosted serve`) gives each provisioned client
a remote-controlled browser on this host: the client logs in to the dashboard,
watches a CDP screencast of *their own* browser, and can take manual control
through validated input forwarding. Navigation is restricted to the client's
configured portal origins; monitoring pauses while the client is in control.
"""
