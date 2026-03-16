
from typing import List
from dots_infrastructure.code_gen.code_gen import CodeGenerator
import json
import os
from dataclasses import dataclass

@dataclass
class FindReplace:
    find : str
    replace : str

def replace_string_in_file(file_path, find_replace : List[FindReplace]):
    if os.path.exists(file_path):
        with open(file_path, 'r') as file:
            filedata = file.read()

        for item in find_replace:
            filedata = filedata.replace(item.find, item.replace)

        with open(file_path, 'w') as file:
            file.write(filedata)

code_generator = CodeGenerator()
with open("input.json", "r") as input_file:
    input_data = input_file.read()

parsed_input_data = json.loads(input_data)
cs_name = code_generator.camel_case(parsed_input_data["name"])
cs_python_name = code_generator.get_python_name(cs_name)
cs_python_base_class_name = code_generator.get_base_class_name(cs_name)

if os.path.exists("src/ExampleCalculationService"):
    os.rename("src/ExampleCalculationService", f"src/{cs_name}")

if os.path.exists(f"src/{cs_name}/calculation_service_test.py"):
    os.rename(f"src/{cs_name}/calculation_service_test.py", f"src/{cs_name}/{cs_python_name}.py")

if os.path.exists("test/test_template.py"):
    os.rename("test/test_template.py", f"test/test_{cs_python_name}.py")

code_generator.code_gen(input=input_data, code_output_dir=f"src/{cs_name}", documentation_ouput_dir="docs")

replace_string_in_file('pyproject.toml', [FindReplace('ExampleCalculationService', cs_name)])
replace_string_in_file(f'src/{cs_name}/{cs_python_name}.py', [FindReplace('CalculationServiceTest', cs_name), FindReplace('CalculationServiceTestBase', f'{cs_name}Base')])
replace_string_in_file(f'test/test_{cs_python_name}.py', [FindReplace('CalculationServiceTest', cs_name), FindReplace('calculation_service_test', cs_python_name), FindReplace('ExampleCalculationService', cs_name)] )
replace_string_in_file('Dockerfile', [FindReplace('<<INSERT_FOLDER_NAME>>', cs_name), FindReplace('<<INSERT_IMPLEMENTATION_PYTHON_FILENAME>>', cs_python_name)] )
