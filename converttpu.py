from pathlib import Path

# blabla.reshape(1,3,2).contiguous().mean(2)
# blabla.reshape(foo(1), 2, bar(foo(3))).contiguous().sum(1)
# blabla.reshape(foo(1), 2, bar(foo(3))).contiguous().sum(1)

for path in Path('./').rglob('**/**/**/**/**/**/*.py'):
	print(path.name)

	insert = '.contiguous()'
	rep = False
	prefixes = ['view', 'reshape']
	with open(path, 'r') as f:
		text = f.read()
		for prefix in prefixes:
			pivot = '.'+prefix+'('
			subject = 0
			amnt = text.count(pivot)

			for i in range(amnt):
				subject = text.index(pivot, subject) + len(pivot)
				cnt = 1

				for k, c in enumerate(text[subject:]):
					if c == '(': cnt += 1
					if c == ')': cnt -= 1
					if cnt == 0: break
				insert_idx = subject + k + 1

				try:
					tmpidx = text.index(insert, insert_idx)
				except ValueError:
					tmpidx = -1
				if tmpidx != insert_idx:
					#print(insert_idx, text.index(insert, insert_idx))
					#print(text[:insert_idx])
					#print(text[insert_idx:])
					text = text[:insert_idx] + insert + text[insert_idx:]
					#print(text)
					rep = True

		text = text.replace(*['.'+_+'(' for _ in prefixes])

	with open(path, 'w') as f:
		f.write(text)
