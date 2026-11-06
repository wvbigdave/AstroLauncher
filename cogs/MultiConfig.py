"""MultiConfig — INI reader/writer that preserves duplicate keys (as lists).

The stock ``configparser`` collapses repeated keys to the last value, which
breaks Astroneer's settings files. This keeps the original contract: sections
are ``[SectionName]`` headers, repeated keys accumulate into lists, and values
are parsed to booleans when they match the known boolean states.
"""

import json
import os

import chardet


class MultiConfig:
    """A small INI accessor exposing its data via :meth:`getdict`."""

    BOOLEAN_STATES = {
        "yes": True,
        "true": True,
        "on": True,
        "no": False,
        "false": False,
        "off": False,
    }

    def getdict(self):
        return self.__dict__.copy()

    def read_dict(self, configDict):
        self.__dict__ = json.loads(json.dumps(configDict), parse_int=str, parse_float=str)

    def read(self, configPath):
        encoding = MultiConfig.get_encoding(configPath)
        with open(configPath, "r", encoding=encoding) as f:
            lines = [line.strip() for line in f.read().split("\n")]
            section = None
            for p in lines:
                parts = p.split("=", 1)
                key, value = parts[0].strip(), parts[1].strip() if len(parts) > 1 else None
                if value is not None:
                    if value != "":
                        if value.lower() in self.BOOLEAN_STATES:
                            value = self._convert_to_boolean(value)
                        current = self.__dict__.get(section) if section is not None else None
                        if current is None:
                            current = self.__dict__[section] = {}
                        if key in current:
                            if isinstance(current[key], str):
                                current[key] = [current[key], value]
                            else:
                                current[key].append(value)
                        else:
                            current[key] = value
                else:
                    if key:
                        section = key[1 : len(key) - 1]
                        self.__dict__[section] = {}

    def write(self, configFile):
        for section, properties in self.__dict__.items():
            if not isinstance(properties, dict):
                continue
            configFile.write(f"[{section}]\n")
            for p, v in properties.items():
                if isinstance(v, list):
                    for item in v:
                        configFile.write(f"{p}={item}\n")
                else:
                    configFile.write(f"{p}={v}\n")

    def update(self, updateWithDict):
        config = MultiConfig()
        config.read_dict(self._update(self.getdict(), updateWithDict))
        return config

    @staticmethod
    def _update(baseDict, updateWithDict):
        tDict = {}
        for key, value in baseDict.items():
            tDict[key] = value
            if key in updateWithDict:
                if isinstance(value, dict):
                    tDict[key] = MultiConfig._update(value, updateWithDict[key])
                else:
                    tDict[key] = updateWithDict[key]
        tDict.update({k: v for k, v in updateWithDict.items() if k not in tDict})
        return tDict

    def overwrite_with(self, filePath, overwriteDict):
        fileConfig = MultiConfig()
        fileConfig.read(filePath)
        ovrConfig = MultiConfig()
        ovrConfig.read_dict(overwriteDict)
        newConfig = fileConfig.update(ovrConfig.getdict())

        encoding = MultiConfig.get_encoding(filePath)
        with open(filePath, "w", encoding=encoding) as configfile:
            newConfig.write(configfile)
        return newConfig

    def baseline(self, filePath, dictBaseline):
        baseConfig = MultiConfig()
        baseConfig.read_dict(dictBaseline)
        fileConfig = MultiConfig()
        fileConfig.read(filePath)
        newConfig = baseConfig.update(fileConfig.getdict())

        encoding = MultiConfig.get_encoding(filePath)
        with open(filePath, "w", encoding=encoding) as configfile:
            newConfig.write(configfile)
        return newConfig

    @staticmethod
    def _convert_to_boolean(value):
        """Return a boolean value translating from the known states."""
        if value.lower() not in MultiConfig.BOOLEAN_STATES:
            raise ValueError("Not a boolean: %s" % value)
        return MultiConfig.BOOLEAN_STATES[value.lower()]

    @staticmethod
    def get_encoding(filePath):
        pathname = os.path.dirname(filePath)
        if pathname and not os.path.exists(pathname):
            os.makedirs(pathname)
        with open(filePath, "a+", encoding="utf_8"):
            pass
        with open(filePath, "rb") as fP:
            rawdata = fP.read()
        result = chardet.detect(rawdata)
        return result["encoding"]
