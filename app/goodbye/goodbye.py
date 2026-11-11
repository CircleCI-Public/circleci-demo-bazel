from lib.strings.strings import capitalize_words, truncate


def farewell(name, max_len=20):
    message = capitalize_words(f"goodbye {name}, see you soon")
    return truncate(message, max_len)


if __name__ == "__main__":
    print(farewell("world"))
