"""Settings for the test suite: the project settings plus a throwaway SECRET_KEY when none is configured."""
from payroll.settings import *  # noqa: F401,F403
from payroll.settings import SECRET_KEY as _SECRET_KEY

SECRET_KEY = _SECRET_KEY or "test-only-secret-key"
