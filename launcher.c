/*
 * Jarvis.app launcher.
 *
 * Tiny native binary that lives inside Jarvis.app/Contents/MacOS so the
 * backend can be started like any normal Mac app (Spotlight, Dock, Login
 * Items). It changes into ~/jarvis_ai and execs the Python backend.
 *
 * Finder launches apps with a minimal PATH, so the python.org framework
 * interpreter is tried explicitly before falling back to whatever python3
 * is on PATH.
 *
 * Build (or just run ./build_app.sh):
 *   clang -O2 -o Jarvis launcher.c
 */

#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define FRAMEWORK_PYTHON \
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"

int main(void) {
    const char *home = getenv("HOME");
    if (!home || !*home) {
        struct passwd *pw = getpwuid(getuid());
        home = pw ? pw->pw_dir : NULL;
    }
    if (!home) {
        fprintf(stderr, "Jarvis launcher: cannot resolve home directory\n");
        return 1;
    }

    char workdir[1024];
    snprintf(workdir, sizeof(workdir), "%s/jarvis_ai", home);
    if (chdir(workdir) != 0) {
        perror("Jarvis launcher: chdir to ~/jarvis_ai failed");
        return 1;
    }

    if (access(FRAMEWORK_PYTHON, X_OK) == 0) {
        execl(FRAMEWORK_PYTHON, FRAMEWORK_PYTHON, "jarvis.py", (char *)NULL);
    }
    execlp("python3", "python3", "jarvis.py", (char *)NULL);

    perror("Jarvis launcher: failed to start Python backend");
    return 1;
}
