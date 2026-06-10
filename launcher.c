#include <unistd.h>
int main(void) {
    char *const args[] = {
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/Users/rino/jarvis_ai/jarvis.py",
        (char *)0
    };
    execv(args[0], args);
    return 1;
}
