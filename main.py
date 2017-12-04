def main() -> int:
    z: int = get42()
    x: int = 30
    if z > x:
        print('Hello World')
    return x

def get42() -> int:
    return 42

if __name__ == "__main__":
    return main()
