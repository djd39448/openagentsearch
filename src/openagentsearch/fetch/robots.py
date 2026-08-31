from urllib.robotparser import RobotFileParser
from typing import Optional


class RobotsPolicy:
    def __init__(self, robots_txt: str, user_agent: str = "openagentsearch"):
        self.robots_parser = RobotFileParser()
        # Parse the robots.txt string using the .parse() method with line splitting
        lines = robots_txt.splitlines()
        self.robots_parser.parse(lines)
    
    def is_allowed(self, url: str) -> bool:
        return self.robots_parser.can_fetch("*", url)