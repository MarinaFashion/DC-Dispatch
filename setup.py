from setuptools import find_packages, setup

setup(
    name="dc_dispatch",
    version="0.1.1",
    description="Sales-informed initial DC dispatch planning for ERPNext",
    author="Marina Trading Company",
    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
)
