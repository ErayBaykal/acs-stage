import inspect
import SPiiPlusPython as sp

for n in ("WriteInteger", "ReadInteger", "UploadBuffer", "LoadBuffer",
          "CompileBuffer", "RunBuffer", "StopBuffer"):
    print(f"{n}{inspect.signature(getattr(sp, n))}\n")
