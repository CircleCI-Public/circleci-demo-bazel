def capitalize_words(s):
    return " ".join(w.capitalize() for w in s.split())

def truncate(s, length):
    return s[:length] + "..." if len(s) > length else s

def repeat(s, n):
    return s * n
