from setuptools import setup, Extension
setup(name="mypkg", version="0.1", packages=["mypkg"],
      ext_modules=[Extension("mypkg._ext", sources=["mypkg/_ext.c"])])
