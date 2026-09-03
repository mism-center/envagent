#define PY_SSIZE_T_CLEAN
#include <Python.h>
static PyObject *answer(PyObject *self, PyObject *args) { return PyLong_FromLong(42); }
static PyMethodDef M[] = {{"answer", answer, METH_NOARGS, ""}, {NULL, NULL, 0, NULL}};
static struct PyModuleDef mod = {PyModuleDef_HEAD_INIT, "_ext", NULL, -1, M};
PyMODINIT_FUNC PyInit__ext(void) { return PyModule_Create(&mod); }
