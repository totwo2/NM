"""
N.M — 企业智能办公助手

完整套装: AgentLoop + TaskRouter + ModelManager + Memory + 自进化 + Web UI
"""

from setuptools import setup, find_packages

with open("requirements.txt") as f:
    requires = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="ai-agent-harness",
    version="2.0.0",
    description="企业级 N.M — OA + IM + 记忆 + 自进化",
    long_description=__doc__,
    author="老高",
    packages=find_packages(include=["harness", "harness.*", "web"]),
    python_requires=">=3.10",
    install_requires=requires,
    entry_points={
        "console_scripts": [
            "harness=harness.cli:main",
            "harness-web=web.server:main_entry",
        ],
    },
    include_package_data=True,
    package_data={"web": ["static/*.html"]},
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.13",
    ],
)
