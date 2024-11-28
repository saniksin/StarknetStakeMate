def format_decimal(value, decimals=18):
    """
    Форматирует значение с учетом десятичного разделителя.
    :param value: Значение в веях (или другой основе 10^decimals).
    :param decimals: Количество десятичных знаков.
    :return: Строка с форматированным значением.
    """
    value_in_decimal = value / (10 ** decimals)
    return f"{value_in_decimal:,.6f}"  
