"""Packages the add-on into dist/<name>-<version>.nvda-addon."""

import configparser
import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ADDON_DIR = os.path.join(ROOT, "addon")


def readManifest():
	parser = configparser.ConfigParser()
	with open(os.path.join(ADDON_DIR, "manifest.ini"), encoding="utf-8") as f:
		parser.read_string("[addon]\n" + f.read())
	return parser["addon"]["name"], parser["addon"]["version"]


def main():
	name, version = readManifest()
	os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
	target = os.path.join(ROOT, "dist", "%s-%s.nvda-addon" % (name, version))
	with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
		for folder, dirs, files in os.walk(ADDON_DIR):
			dirs[:] = [d for d in dirs if d != "__pycache__"]
			for file in files:
				if file.endswith((".pyc", ".pyo")):
					continue
				path = os.path.join(folder, file)
				z.write(path, os.path.relpath(path, ADDON_DIR))
	print(target)


if __name__ == "__main__":
	main()
