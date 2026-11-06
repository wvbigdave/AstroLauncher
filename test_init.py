import sys

sys.path.insert(0, "/home/dave/AstroLauncher")

from AstroLauncher import AstroLauncher
from cogs.AstroLogging import AstroLogging

AstroLogging.setup_logging()
l = AstroLauncher(astroPath="/home/dave/astroneer-server", disable_auto_update=True)
print("INIT-OK astroPath=", l.astroPath)
