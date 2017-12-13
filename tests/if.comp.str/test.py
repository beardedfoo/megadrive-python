import sys
foo: str = 'foo'
bar: str = 'bar'

if __name__ == '__main__':
    if foo != 'foo':
        sys.exit(1)

    if foo == bar:
        sys.exit(2)

    sys.exit(42)
