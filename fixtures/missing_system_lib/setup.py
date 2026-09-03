from setuptools import setup, Extension
# Compiling this needs libxml2-dev. Nothing else in the repo does.
setup(name="needsxml", version="0.1", py_modules=["needsxml"],
      ext_modules=[Extension("needsxml_ext", sources=["ext.c"])])
