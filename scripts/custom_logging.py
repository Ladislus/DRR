from datetime  import datetime
from logging   import Logger, Formatter, StreamHandler, FileHandler, LogRecord, CRITICAL, ERROR, WARNING, INFO, DEBUG, disable as disable_logging, getLogger, getLevelNamesMapping
from pathlib   import Path
from sys       import stdout
from tempfile  import gettempdir
from threading import Lock

_console = None
_lock    = Lock()

def CONSOLE():
    with _lock:
        global _console
        if _console is None:
            from rich.console import Console
            _console = Console()
    return _console


class CustomDater( Formatter ):
    # Completely ignore the provided format
    def formatTime( self, record: LogRecord, datefmt: str | None = None ) -> str:
        dt = datetime.fromtimestamp( record.created ).astimezone()
        return f'{ dt.strftime( "%d/%m/%Y - %H:%M:%S" ) }.{int( record.msecs ):03d} { dt.strftime( "%z" ) }/{ dt.strftime( "%Z" ) }'

class FileFormatter( CustomDater ):
    pass

class ConsoleFormatter( CustomDater ):
    """Formateur ajoutant des couleurs ANSI si la sortie est un TTY."""

    # Couleurs ANSI configurables
    COLORS = {
        DEBUG   : "\033[36m",  # Cyan
        INFO    : "\033[34m",  # Bleu
        WARNING : "\033[33m",  # Jaune
        ERROR   : "\033[31m",  # Rouge
        CRITICAL: "\033[1;31m" # Rouge gras
    }
    RESET = "\033[0m"

    RICH_COLORS = {
        DEBUG   : "bold magenta",
        INFO    : "bold blue",
        WARNING : "bold yellow",
        ERROR   : "bold red",
        CRITICAL: "blink bold red"
    }

    def __init__( self, fmt: str, use_color: bool, use_rich: bool, **kwargs ):
        super().__init__( fmt = fmt, **kwargs )
        self.use_color = use_color
        self.use_rich  = use_rich

    def format( self, record: LogRecord ) -> str:
        record.short_name = record.name.split( '.' )[ -1 ]

        formatted_msg = super().format( record )

        if self.use_color:
            if self.use_rich:
                if record.levelno in self.RICH_COLORS:
                    from rich.markup import escape
                    return f'[{ self.RICH_COLORS[ record.levelno ] }]{ escape( formatted_msg ) }[/{ self.RICH_COLORS[ record.levelno ] }]'
            elif record.levelno in self.COLORS:
                return f'{ self.COLORS[ record.levelno ] }{ formatted_msg }{ self.RESET }'

        return formatted_msg

LOG_LEVELS = frozenset( [ 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL' ] )

def setup_logging(
    logger_name  : str  | None = None,
    console_level: str  | None = 'INFO',
    file_level   : str  | None = 'DEBUG',
    file_path    : Path | None = None,
    file_name    : str  | None = None,
    log_in_temp  : bool        = True,
    use_rich     : bool        = True,
    quiet        : bool        = False
) -> Logger :
    # Global deactivation
    if quiet:
        getLogger().info( 'All logging disabled !' )
        disable_logging( CRITICAL )
        return getLogger( logger_name )

    logger = getLogger( logger_name )

    # Clean previous handler (in case of multiple calls)
    logger.handlers.clear()

    # Do not propagate configuration to root
    if logger_name is not None:
        logger.propagate = False

    # Compute lowest level
    active_levels: list[ int ] = []
    if console_level:
        active_levels.append( getLevelNamesMapping().get( console_level.upper(), INFO ) )
    if file_level:
        active_levels.append( getLevelNamesMapping().get( file_level.upper(), DEBUG ) )
    logger.setLevel( min( active_levels ) if active_levels else CRITICAL )

    # 1. Console handler
    if console_level:
        console_handler = None
        if use_rich:
            try:
                from rich.logging     import RichHandler
                from rich.highlighter import NullHighlighter

                console_handler = RichHandler(
                    console         = CONSOLE(),
                    show_time       = False,
                    show_level      = False,
                    show_path       = False,
                    highlighter     = None,
                    # highlighter     = NullHighlighter(),
                    markup          = True,
                    rich_tracebacks = True
                )
            except ImportError as ex:
                from sys import stderr
                use_rich = False
                print( f'Could not import Rich module, fallback to default logger (cause: "{ ex }")', file = stderr )
        if console_handler is None:
            console_handler = StreamHandler( stdout )
        console_handler.setLevel( getLevelNamesMapping().get( console_level.upper(), INFO  ) )
        console_handler.setFormatter(
            ConsoleFormatter(
                fmt = "( %(asctime)s ) [ %(levelname)-8s ] %(filename)s:%(lineno)d - %(funcName)s - %(message)s",
                use_color = stdout.isatty(),
                use_rich = use_rich
            )
        )
        logger.addHandler( console_handler )

        logger.info( f'Logger "{ logger_name }" setup with CONSOLE: { console_level }' )

    # 2. File handler
    if file_level:
        if file_name is None:
            file_name = logger_name

        if file_path is None:
            file_path = Path( '.' )

        log_directory = Path( gettempdir() ).resolve() / file_path if log_in_temp else Path( file_path )
        log_file      = log_directory / f'{ file_name }_{ datetime.now().astimezone().strftime( "%d_%m_%Y_%H_%M_%S_%Z" ) }.log'

        log_directory.mkdir( parents = True, exist_ok = True )

        file_handler = FileHandler( log_file, encoding = 'utf-8' )
        file_handler.setLevel( getLevelNamesMapping().get( file_level.upper(), DEBUG ) )
        file_handler.setFormatter( FileFormatter( fmt = "( %(asctime)s ) { %(filename)s:%(lineno)d @ %(funcName)s - PID: %(process)d - TID: %(thread)d - %(name)s } [ %(levelname)s ] - %(message)s" ) )
        logger.addHandler( file_handler )

        logger.info( f'Logger "{ logger_name }" setup with FILE: { file_level } @ "{ log_file.absolute() }"' )

    return logger