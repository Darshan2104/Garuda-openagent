def add_one(value):
    result = value + 1
    return result


number = 41
nested = [[1, 2], [3, 4]]
large = list(range(500))
text = "x" * 5000
answer = add_one(number)  # breakpoint
print("answer:", answer, flush=True)
print("z" * 30000, flush=True)
