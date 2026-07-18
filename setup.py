"""Python setup.py for i3expo package"""
import io
import os
from setuptools import find_packages, setup#, Extension


# note the version is managed by zest.releaser:
version = "0.0.8.dev0"


# prtscn_py = Extension(
    # 'prtscn_py',
    # sources=['prtscn_py.c'],
    # libraries=['X11'],
    # language='c',
# )

def read(*paths, **kwargs):
    """Read the contents of a text file safely.
    >>> read("README.md")
    ...
    """

    content = ""
    with io.open(
        os.path.join(os.path.dirname(__file__), *paths),
        encoding=kwargs.get("encoding", "utf8"),
    ) as open_file:
        content = open_file.read().strip()
    return content


def read_requirements(path):
    return [
        line.strip()
        for line in read(path).split("\n")
        if not line.startswith(('"', "#", "-", "git+"))
    ]


setup(
    name="i3expo",
    version=version,
    description="display current i3 workspaces",
    url="https://github.com/laur89/i3expo",
    long_description=read("README.md"),
    long_description_content_type="text/markdown",
    author="Laur",
    license="MIT",
    # packages=["i3expo"],
    packages=find_packages(exclude=["tests", ".github", "img"]),
    package_data={'i3expo': ['*.so']},
    # ext_modules=[prtscn_py],
    # python_requires='>=3.7',
    install_requires=read_requirements("requirements.txt"),
    entry_points={
        "console_scripts": ["i3expo = i3expo:run"]
    },
    extras_require={"test": read_requirements("requirements-test.txt")},
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
    ]
)
