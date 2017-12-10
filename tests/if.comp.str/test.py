import C
foo: str = 'foo'
bar: str = 'bar'

if __name__ == '__main__':
    if foo != 'foo':
        C.exit(1)

    if foo == bar:
        C.exit(2)

    C.exit(42)
