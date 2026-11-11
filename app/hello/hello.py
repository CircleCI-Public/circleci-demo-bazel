from lib.math.math import add
from lib.strings.strings import capitalize_words


def greet(name):
    lucky = add(6, 1)
    return capitalize_words(f"hello {name}, your lucky number is {lucky}")


if __name__ == "__main__":
    print(greet("world"))
