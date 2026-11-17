import json, sys
from . import pipeline

def main():
    print(json.dumps(pipeline.run(), ensure_ascii=False))

if __name__ == "__main__":
    sys.exit(main())
