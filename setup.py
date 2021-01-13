import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="thiscovery-lib",  # Replace with your own username
    version="2021.1.5",
    author="Thiscovery team",
    author_email="support@thiscovery.org",
    description="Thiscovery library",
    install_requires=[
        'boto3',
        'botocore',
        'python-dateutil',
        'epsagon',
        'python-json-logger',
        'requests',
        'validators',
    ],
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/THIS-Institute/thiscovery-lib",
    package_data={
        'thiscovery_lib': ['countries.json'],
    },
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
)