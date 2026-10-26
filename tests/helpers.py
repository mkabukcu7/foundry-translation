from functools import lru_cache


@lru_cache
def indexed_functions():
    import function_app
    return {f.get_function_name(): f for f in function_app.app.get_functions()}
