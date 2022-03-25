from setuptools import setup

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="para_mlp",
    version="1.1.0",
    author="Taiki Iwamura",
    author_email="takki.0206@gmail.com",
    description="paramagnetic machine learning potential package",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/iwamura-lab/para-mlp",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    packages=["para_mlp"],
    python_requires=">=3.7",
)
