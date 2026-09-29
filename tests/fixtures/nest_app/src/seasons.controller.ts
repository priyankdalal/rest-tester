import { Body, Controller, Get, Post, Query } from '@nestjs/common';

@Controller('seasons')
export class SeasonsController {
  @Get(':seasonId')
  findOne(@Query('include') include: string) {
    return {};
  }

  @Post()
  create(@Body() payload: CreateSeasonDto) {
    return payload;
  }
}
