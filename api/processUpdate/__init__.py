import logging
import azure.functions as func

def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("processUpdate function called")

    return func.HttpResponse(
        "processUpdate API is working",
        status_code=200
    )
