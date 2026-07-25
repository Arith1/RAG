import inspect

class Loader:
    @staticmethod
    def load_a(): return "aaa"
    def load_b(self): return "bbb"

obj = Loader()
# 自动生成 {方法名: 绑定方法} 的映射
func_map = dict(inspect.getmembers(obj, predicate=inspect.isfunction))

print(func_map)

s = "abce.a"

func = "load_" + s.split(".")[-1]

valid_file_types = [s.split("_")[-1] for s in list(func_map.keys())]
print(valid_file_types)
print(func_map[func]())  # aaa