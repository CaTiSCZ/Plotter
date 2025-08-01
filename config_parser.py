import logging
from logger import application_logger

import os
import configparser
from contextlib import AbstractContextManager
from pathlib import Path
import functools
from typing import Any
from callback_engine import Callback_engine

class ConfigParser(AbstractContextManager, configparser.ConfigParser, Callback_engine):
    ENCODING = 'utf-8'
    SECTION_MODIFICATORS = 'setlist appendlist  extendlist setboolean'.split()
    def __init__(self, config_file: Path, *args, create_empty: bool = True, no_raise: bool = True, initializator = None, **kwargs) -> None:
        self._logger = logging.getLogger(__class__.__name__ if application_logger is None else f'{application_logger}.{__class__.__name__}')
        self._modified = False
        AbstractContextManager.__init__(self)
        configparser.ConfigParser.__init__(self, *args, **kwargs)
        Callback_engine.__init__(self)
        try:
            if config_file.exists():
                self.read(config_file, ConfigParser.ENCODING)
                initializator = None
            elif create_empty:
                for p in reversed(config_file.parents):
                    if p.is_file():
                        break
                    if not p.exists():
                        os.mkdir(p)
                else:
                    with open(config_file, 'w', encoding=ConfigParser.ENCODING):
                        pass
            else:
                config_file = None
        except:
            config_file = None
            if not no_raise:
                raise
        finally:
            self._config_file = config_file
            if initializator is not None:
                initializator(self)
            else:
                self._modified = False
        self._mock_sections_proxy().process()
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.save()
        return False

    def _mock_sections_proxy(self):
        class Ctx_mgr_proxy:
            def __init__(self, parser):
                self.parser = parser
                self.old_sections = []
            def __enter__(self):
                self.old_sections = self.parser.sections()
            def __exit__(self, *_):
                self.process()
                return False
            def process(self):
                for s in frozenset(self.parser.sections()).difference(self.old_sections):
                    for fcn in ConfigParser.SECTION_MODIFICATORS:
                        setattr(self.parser[s], fcn, functools.partial(getattr(self.parser, fcn), s))
        return Ctx_mgr_proxy(self)

    def _read(self, fp, fpname):
        self._modified = True
        with self._mock_sections_proxy():
            return super()._read(fp, fpname)

    def __setitem__(self, key: str, value: Any) -> None:
        self._modified = True
        return super().__setitem__(key, value)
    
    def __delitem__(self, key: str) -> None:
        self._modified = True
        return super().__delitem__(key)

    def set(self, section: str, option: str, value: str | None = None) -> None:
        self._modified = True
        self._logger.debug('["%s"]["%s"] = "%s"', section, option, value)
        self._call(f'onChange[{section}][{option}]', section, option, value)
        return super().set(section, option, value)
    
    def get(self, section, option, *, raw=False, vars=None, fallback=configparser._UNSET):
        value = super().get(section, option, raw=raw, vars=vars, fallback=fallback)
        self._logger.debug('["%s"]["%s"] is "%s"', section, option, value)
        return value
    
    def add_section(self, section: str) -> None:
        self._modified = True
        with self._mock_sections_proxy():
            return super().add_section(section)
    
    def remove_option(self, section: str, option: str) -> bool:
        self._modified = True
        return super().remove_option(section, option)
    
    def remove_section(self, section: str) -> bool:
        self._modified = True
        return super().remove_section(section)

    def getlist(self, section, option, *, raw=False, vars=None,
               fallback=configparser._UNSET, converter = str, **kwargs):
        values = self._get_conv(section, option, lambda s: s.split('\n'), raw=raw, vars=vars,
                              fallback=fallback, **kwargs)
        if values and not values[0]:
            values = values[1:]
        return list(map(converter, values))

    def setlist(self, senction, option, values):
        self.set(senction, option, '\n'+'\n'.join(values))

    def appendlist(self, section, option, value):
        self.extendlist(section, option, [value])

    def extendlist(self, section, option, values):
        orig = self.getlist(section, option) if self.has_option(section, option) else []
        orig.extend(values)
        self.setlist(section, option, orig)

    def setboolean(self, section, option, value: bool):
        self.set(section, option, 'yes' if value else 'no')

    def save(self, force = False):
        if force or self._modified:
            if self._config_file is not None:
                with open(self._config_file, 'w', encoding=ConfigParser.ENCODING) as f:
                    self.write(f)
                self._modified = False
            else:
                raise FileNotFoundError('No .ini file is configured.')
            
    def dump(self, sink = None):
        res = ''
        for name, section in self.items():        
            res += f'[{name}]\n'
            for option in section:
                value = section[option].replace('\n', '\n\t\t')
                res += f'\t{option} = {value}\n'
        if sink is not None:
            sink.write(res)
        return res
    
    def guaranteed_options(self, sections: dict[str, dict[str, str]]) -> None:
        for section, options in sections.items():
            if not self.has_section(section):
                self.add_section(section)
            for option, value in options.items():
                if option and not self.has_option(section, option):
                    if isinstance(value, (list, tuple)):
                        self.setlist(section, option, value)
                    else:
                        self.set(section, option, value)
