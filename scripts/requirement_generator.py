requirements = """/** @requirement REQ_00001_a_1
  * Do blablabla
  */

/** @requirement REQ_00002_a_1
  * Do something else
  */

/** @requirement REQ_00003_a_1
  * Whatever
  */
"""

from custom_logging import setup_logging
from rich.console   import Console

from sys import argv

LOGGER = setup_logging( 'RequirementGenerator' )

def main():
  with open( argv[ 1 ], 'w' ) as file:
      with Console().status( '[bold cyan] writting requirements...' ):
        file.write( requirements )
      LOGGER.info( 'Requirements written' )

if __name__ == '__main__':
    main()