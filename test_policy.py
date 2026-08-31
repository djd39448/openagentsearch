#!/usr/bin/env python3

"""Simple test to verify fetch policy is working correctly"""

import sys
sys.path.insert(0, '.')

from openagentsearch.fetch.policy import robots_allows

def main():
    # Test cases that should pass 
    print("Testing allowlisted URLs...")
    assert robots_allows("https://docs.python.org/3/tutorial/index.html")
    assert robots_allows("http://docs.python.org/2.7/")
    assert robots_allows("https://docs.python.org/")
    print("✓ Allowlisted URLs all pass")
    
    # Test cases that should fail
    print("Testing non-allowlisted URLs...")
    assert not robots_allows("https://example.com/")
    assert not robots_allows("http://google.com/search")
    assert not robots_allows("https://github.com/user/repo")
    print("✓ Non-allowlisted URLs all fail")
    
    # Test cases for different schemes and ports (the previously broken one)
    print("Testing different schemes and ports...")
    assert robots_allows("http://docs.python.org:80/3/")
    assert robots_allows("https://docs.python.org:443/3/")
    assert not robots_allows("http://example.com:8080/")
    print("✓ Different schemes and ports all handled correctly")
    
    print("\n🎉 All tests passed successfully!")

if __name__ == "__main__":
    main()