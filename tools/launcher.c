/* Compartment.app's executable.
 *
 * This runs the embedded interpreter IN PROCESS, via Py_BytesMain, instead of
 * exec'ing Contents/MacOS/python. That distinction is the whole reason this
 * file exists.
 *
 * A shell launcher that exec's a differently-named binary leaves the running
 * image no longer matching CFBundleExecutable. Everything still *looks* fine
 * from inside the process - AppKit reports the status item as visible, with an
 * image, with a window - but the menu bar never gives it a slot: the item is
 * created at x=0 and simply never appears. Launch the same bundle straight
 * from a shell, where there is no LaunchServices registration to contradict,
 * and the icon shows up, which is what makes the bug so confusing to chase.
 *
 * Linking libpython and calling Py_BytesMain keeps the process image at
 * Contents/MacOS/Compartment for its whole life, which is what LaunchServices
 * registered and what the menu bar wants to see. It is also what py2app and
 * PyInstaller do, for the same reason.
 *
 * Built by tools/build_macos_app.py; not used outside the bundle.
 */
#include <Python.h>
#include <libgen.h>
#include <mach-o/dyld.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    char exe[4096];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0) return 70;   /* EX_SOFTWARE */
    char *macos = dirname(exe);                  /* .../Contents/MacOS */

    /* PYTHONHOME is what ties the interpreter to the runtime in Resources/.
       Without it CPython infers a prefix of Contents/ and finds no stdlib. */
    char home[4608];
    snprintf(home, sizeof home, "%s/../Resources/runtime", macos);
    setenv("PYTHONHOME", home, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);   /* never write into a signed bundle */
    unsetenv("PYTHONPATH");
    unsetenv("PYTHONSTARTUP");

    char **py = calloc((size_t)argc + 5, sizeof *py);
    if (py == NULL) return 71;                              /* EX_OSERR */
    int n = 0;
    py[n++] = argv[0];
    py[n++] = "-m";
    py[n++] = "compartment.cli";
    py[n++] = "menubar";
    for (int i = 1; i < argc; i++) {
        /* LaunchServices can append a -psn_0_… process serial number, which
           is not ours to interpret and is not a flag the CLI knows. */
        if (strncmp(argv[i], "-psn_", 5) == 0) continue;
        py[n++] = argv[i];
    }
    py[n] = NULL;
    return Py_BytesMain(n, py);
}
