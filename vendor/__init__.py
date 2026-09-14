# Packaging vehicle only (issue #204): lets setuptools ship the vendored
# three.js in vendor/three-0.160.0 as package data so viz's __file__-relative
# _VENDOR lookup keeps working from site-packages. No code lives here.
