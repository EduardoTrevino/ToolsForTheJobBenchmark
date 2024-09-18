import argparse
import json
import os
import litellm
import re

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default="openai/neulab/gpt-4o-2024-05-13", help='Model name')
    parser.add_argument('--openai_key', type=str, required=True, help='OpenAI API key')
    parser.add_argument('--input_query_file', type=str, required=True, help='Input query file')
    parser.add_argument('--output_answer_file', type=str, required=True, help='Output answer file')
    parser.add_argument('--tool_root_dir', type=str, required=True, help='Directory with tool definitions')
    args = parser.parse_args()

    # Read input queries
    with open(args.input_query_file, 'r') as f:
        queries = json.load(f)

    results = []

    for query_data in queries:
        query_id = query_data.get('query_id', None)
        print(f"\nProcessing query_id: {query_id}")

        # Process the query
        answer = process_query(query_data, args)

        results.append({
            'query_id': query_id,
            'query': query_data['query'],
            'answer': answer
        })

    # Write results to output file
    with open(args.output_answer_file, 'w') as f:
        json.dump(results, f, indent=2)


def process_query(query_data, args):
    query_text = query_data['query']
    print(f"\nProcessing query: {query_text}")
    model_name = args.model

    # Check if the model supports function calling
    try:
        supports_function_calling = litellm.supports_function_calling(model=model_name)
        print(f"Model '{model_name}' supports function calling: {supports_function_calling}")
    except Exception as e:
        print(f"Exception when checking function calling support: {e}")
        supports_function_calling = False

    # Define the "Finish" function and add it to the functions list
    finish_func = {
        "name": "Finish",
        "description": "If you believe that you have obtained a result that can answer the task, please call this function to provide the final answer. Alternatively, if you recognize that you are unable to proceed with the task in the current state, call this function to restart. Remember: you must ALWAYS call this function at the end of your attempt, and the only part that will be shown to the user is the final answer, so it should contain sufficient information.",
        "parameters": {
            "type": "object",
            "properties": {
                "return_type": {
                    "type": "string",
                    "enum": ["give_answer","give_up_and_restart"],
                },
                "final_answer": {
                    "type": "string",
                    "description": "The final answer you want to give the user. You should have this field if \"return_type\"==\"give_answer\"",
                }
            },
            "required": ["return_type"],
        }
    }
        # Load functions
    functions = load_functions(args.tool_root_dir, query_data)
    print(f"Loaded {len(functions)} functions.")

    # Optionally, print the functions loaded
    for function in functions:
        print(f"Function loaded: {function['name']}")

    functions.append(finish_func)

        # If the model does not support function calling, set add_function_to_prompt
    if not supports_function_calling:
        litellm.add_function_to_prompt = True
    else:
        litellm.add_function_to_prompt = False

    # Prepare messages
    system_message = """"You are a helpful assistant that can use tools to help the user.

You should use functions to help handle the real-time user queries. Remember:
1. ALWAYS call the "Finish" function at the end of the task. The final answer should contain enough information to show to the user. If you can't handle the task, or you find that function calls always fail (the function is not valid now), use function Finish->give_up_and_restart.
2. Do not use original tool names, use only subfunctions' names.
When you need to use a tool, output the function call in the following JSON format on a separate line:

```json
{ "function": "function_name", "arguments": { ... } }
"""
    if not supports_function_calling:
        # Include function definitions in the system prompt
        function_descriptions = "\n".join([format_function_for_prompt(f) for f in functions])
        system_message += "\n\nAvailable tools:\n" + function_descriptions

    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": query_text}
    ]

    assistant_reply = None
    for step in range(10):  # Limit the number of steps
        print(f"\n--- Step {step + 1} ---")
        # Call the model
        print("Sending messages to the model:")

        for message in messages:
            print(f"{message['role']}: {message.get('content', '')}")

        response = litellm.completion(
            api_key=args.openai_key,
            base_url="https://cmu.litellm.ai",
            model=args.model,
            messages=messages,
            functions=functions if supports_function_calling else None
        )

        # Extract the assistant's reply
        assistant_message = response.choices[0].message
        print(f"\nAssistant message received: {assistant_message}")
        print ("Raw Response :", response)

        # Convert assistant_message to dict format
        assistant_message_dict = {
            'role': assistant_message.role,
            'content': assistant_message.content,
        }

        if supports_function_calling and assistant_message.function_call is not None:
            assistant_message_dict['function_call'] = {
                'name': assistant_message.function_call.name,
                'arguments': assistant_message.function_call.arguments
            }
            messages.append(assistant_message_dict)

            # Assistant is calling a function
            function_name = assistant_message.function_call.name
            function_args_str = assistant_message.function_call.arguments
            print(f"Assistant is calling function '{function_name}' with arguments: {function_args_str}")

            try:
                function_args = json.loads(function_args_str)
            except json.JSONDecodeError as e:
                print(f"Error decoding function arguments: {e}")
                function_args = {}

            # Execute the function
            function_response = execute_function(function_name, function_args, args.tool_root_dir, query_data)
            print(f"Function response: {function_response}")

            # Add the function response to messages
            messages.append({
                "role": "function",
                "name": function_name,
                "content": function_response
            })
        else:
            # Try to extract function call from assistant's content
            print("Printing assistant message dict[content]:", assistant_message_dict["content"])
            print("Printing assistant_message.content", assistant_message.content)

            function_call = extract_function_call_from_content(assistant_message_dict.content)
            if function_call:
                function_name = function_call.get('function')
                function_args = function_call.get('arguments', {})
                print(f"Assistant is calling function '{function_name}' with arguments: {function_args}")

                # Execute the function
                function_response = execute_function(function_name, function_args, args.tool_root_dir, query_data)
                print(f"Function response: {function_response}")

                # Add the assistant's message and function response to messages
                messages.append(assistant_message_dict)
                messages.append({
                    "role": "function",
                    "name": function_name,
                    "content": function_response
                })

                # Check if the function called is "Finish" and handle accordingly
                if function_name == "Finish":
                    if function_args.get("return_type") == "give_answer":
                        assistant_reply = function_args.get("final_answer", "")
                        print(f"Assistant final reply: {assistant_reply}")
                        break
                    elif function_args.get("return_type") == "give_up_and_restart":
                        print("Assistant chose to give up and restart.")
                        assistant_reply = "Assistant chose to give up and restart."
                        break
                else:
                    # Continue to the next step
                    continue
            else:
                # Assistant did not call a function
                print("Assistant did not call a function. Reminding assistant to use functions.")
                messages.append(assistant_message_dict)

                # Add a message to remind the assistant
                messages.append({
                    "role": "assistant",
                    "content": "Remember to use the provided functions to complete the task, and to call the 'Finish' function when you are ready to provide the final answer."
                })
    
    if assistant_reply is None:
        print("Assistant did not provide a final reply within the allowed steps.")
        assistant_reply = "Assistant did not provide a final reply."

    return assistant_reply


def extract_function_call_from_content(content):
    """
    Use regex to find a function call in the specified format.
    """
    pattern = r'json\s*([\s\S]*?)'
    print("Data being passeed to extract_function_call_from_content", content)
    match = re.search(pattern, content, re.DOTALL)
    
    if match:
        function_call_str = match.group(1)
        print("Here is what the function call string match was", function_call_str)
        try:
            function_call = json.loads(function_call_str)
            return function_call
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
    
    return None

def load_functions(tool_root_dir, query_data):
    import os
    import json
    functions = []

    # Only load functions specified in the query_data
    for api in query_data.get('api_list', []):
        category_name = api['category_name']
        tool_name = api['tool_name']
        api_name = api['api_name']
        tool_json_path = os.path.join(tool_root_dir, category_name, tool_name + '.json')
        
        if not os.path.exists(tool_json_path):
            print(f"Tool JSON file not found at {tool_json_path}")
            continue

        with open(tool_json_path, 'r') as f:
            tool_data = json.load(f)

        # Find the specific API
        api_data = None
        for api_item in tool_data.get('api_list', []):
            if api_item['name'] == api_name:
                api_data = api_item
                break

        if api_data:
            # Convert API to OpenAI function format
            function = api_to_openai_function(api_data, tool_name)
            functions.append(function)
        else:
            print(f"API '{api_name}' not found in tool '{tool_name}'")

    return functions

def api_to_openai_function(api, tool_name):
    function = {
        "name": api['name'],
        "description": api.get('description', ''),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }

    # Handle required parameters
    required_params = api.get('required_parameters', [])
    for param in required_params:
        param_name = param['name']
        param_type = map_param_type(param['type'])
        param_desc = param.get('description', '')
        param_schema = {
            "type": param_type,
            "description": param_desc
        }
        if param_type == "array":
            # Include 'items' in the schema
            param_schema["items"] = {"type": "string"}  # Assuming array of strings
        function['parameters']['properties'][param_name] = param_schema
        function['parameters']['required'].append(param_name)

    # Handle optional parameters
    optional_params = api.get('optional_parameters', [])
    for param in optional_params:
        param_name = param['name']
        param_type = map_param_type(param['type'])
        param_desc = param.get('description', '')
        param_schema = {
            "type": param_type,
            "description": param_desc
        }
        if param_type == "array":
            # Include 'items' in the schema
            param_schema["items"] = {"type": "string"}  # Assuming array of strings
        function['parameters']['properties'][param_name] = param_schema
        # Optional parameters are not added to 'required'

    return function

def map_param_type(param_type):
    type_map = {
        "NUMBER": "number",
        "INTEGER": "integer",
        "STRING": "string",
        "BOOLEAN": "boolean",
        "LIST": "array",
        "OBJECT": "object"
    }
    return type_map.get(param_type.upper(), "string")

def execute_function(function_name, function_args, tool_root_dir, query_data):
    # function_name is the name of the function to execute (api_name)
    # function_args are the arguments for the function
    # query_data contains the 'api_list' with 'category_name' and 'tool_name'
    
    # Find the corresponding API entry in query_data
    api_entry = None
    for api in query_data.get('api_list', []):
        if api['api_name'] == function_name:
            api_entry = api
            break
    
    if not api_entry:
        print(f"API '{function_name}' not found in query data.")
        return f"API '{function_name}' not found in query data."
    
    category_name = api_entry['category_name']
    tool_name = api_entry['tool_name']
    
    # Construct the path to api.py
    api_py_path = os.path.join(tool_root_dir, category_name, tool_name, 'api.py')
    if not os.path.exists(api_py_path):
        print(f"API file not found at {api_py_path}")
        return f"API file not found at {api_py_path}"
    
    # Import the module from api.py
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"{category_name}.{tool_name}.api", api_py_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"Error loading module: {e}")
        return f"Error loading module: {e}"
    
    # Get the function from the module
    func = getattr(module, function_name, None)
    if not func:
        print(f"Function '{function_name}' not found in module.")
        return f"Function '{function_name}' not found in module."
    
    # Execute the function with provided arguments
    try:
        print(f"Executing function '{function_name}' with arguments: {function_args}")
        result = func(**function_args)
        print(f"Function '{function_name}' returned: {result}")
        # If the result is not serializable, convert it to string
        if not isinstance(result, (str, int, float, dict, list)):
            result = str(result)
        return json.dumps(result)
    except Exception as e:
        print(f"Error executing function '{function_name}': {e}")
        return f"Error executing function '{function_name}': {e}"
    
def format_function_for_prompt(function):
    """
    Format the function definition for inclusion in the prompt.
    """
    params = function['parameters']['properties']
    required = function['parameters'].get('required', [])
    param_descriptions = []
    for name, details in params.items():
        param_str = f"- {name} ({details['type']})"
        if name in required:
            param_str += " [required]"
        if 'description' in details and details['description']:
            param_str += f": {details['description']}"
        param_descriptions.append(param_str)
    param_text = "\n".join(param_descriptions)
    return f"Function '{function['name']}': {function['description']}\nParameters:\n{param_text}\n"

if __name__ == '__main__':
    main()
